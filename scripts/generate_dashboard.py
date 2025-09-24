import base64, json, os, re
from pathlib import Path
import requests
import tomllib as tomli  # Python 3.11 'tomllib'
import yaml
from jinja2 import Template

ORG = os.environ.get("ORG","netboxlabs")
TOKEN = os.environ["GH_TOKEN"]
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}

# Heuristics for detecting test workflows and jobs
TEST_WORKFLOW_RE = re.compile(
    r"(test|tests|pytest|unit|integration|e2e|acceptance|regress|smoke|playwright|behave|bdd|qa|automation|testsuite|test-suite)",
    re.I,
)
NON_TEST_HINT = re.compile(
    r"(doc|docs|page|pages|website|release|docker|publish|deploy|package|lint|format|codeql)",
    re.I,
)

TEST_JOB_RE = re.compile(
    r"(test|tests|pytest|unit|integration|e2e|acceptance|regress|smoke|playwright|behave|bdd|qa|automation|testsuite|test-suite|cypress|jest)",
    re.I,
)
NON_TEST_JOB_HINT = re.compile(
    r"(doc|docs|page|pages|website|release|docker|publish|deploy|package|lint|format|codeql|upload|allure|qase|report|artifact|cache|setup|install|build)",
    re.I,
)

def gh(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()

def list_repos(org):
    """Return all repos visible to the token. Tries multiple endpoints and logs what happens."""
    repos, seen = [], set()

    def add_many(rs):
        for r in rs or []:
            owner = (r.get("owner") or {}).get("login", "")
            name = r.get("name")
            full = r.get("full_name") or (f"{owner}/{name}" if owner and name else None)
            if owner.lower() == org.lower() and full and full not in seen:
                seen.add(full)
                repos.append(r)

    # 1) Org endpoint (best when allowed)
    for page in range(1, 6):
        try:
            data = gh(
                f"https://api.github.com/orgs/{org}/repos",
                params={"per_page": 100, "page": page, "type": "all", "sort": "full_name"},
            )
            if not data:
                break
            add_many(data)
        except requests.exceptions.HTTPError as e:
            sc = getattr(e.response, "status_code", None)
            print(f"[list_repos] org repos page {page} -> HTTP {sc}; falling back")
            if sc in (403, 404):
                break  # skip to user/search fallbacks
            else:
                raise

    # 2) User endpoint (works for many classic PATs)
    for page in range(1, 6):
        try:
            data = gh(
                "https://api.github.com/user/repos",
                params={
                    "per_page": 100,
                    "page": page,
                    "affiliation": "owner,organization_member,collaborator",
                },
            )
            if not data:
                break
            add_many(data)
        except requests.exceptions.HTTPError as e:
            sc = getattr(e.response, "status_code", None)
            print(f"[list_repos] user repos page {page} -> HTTP {sc}")
            if sc in (403, 404):
                break
            else:
                raise

    # 3) Search fallback (returns both public and private you can access)
    for page in range(1, 6):
        try:
            data = gh(
                "https://api.github.com/search/repositories",
                params={"q": f"org:{org}", "per_page": 100, "page": page},
            )
            items = data.get("items", [])
            if not items:
                break
            add_many(items)
        except requests.exceptions.HTTPError as e:
            sc = getattr(e.response, "status_code", None)
            print(f"[list_repos] search repos page {page} -> HTTP {sc}")
            if sc in (403, 404):
                break
            else:
                raise

    print(f"[list_repos] collected {len(repos)} repos for org '{org}'")
    return repos

def read_file_version(owner, repo, path, ref):
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    try:
        data = gh(url, params={"ref": ref})
        content = base64.b64decode(data["content"]).decode("utf-8")
    except Exception:
        return None
    if path.endswith("pyproject.toml"):
        try:
            v = tomli.loads(content).get("project",{}).get("version")
            return v
        except Exception:
            return None
    if path.endswith("package.json"):
        try:
            return json.loads(content).get("version")
        except Exception:
            return None
    if path.lower().endswith("chart.yaml"):
        try:
            return yaml.safe_load(content).get("version")
        except Exception:
            return None
    if path.endswith(("setup.cfg","setup.py","VERSION")):
        m = re.search(r"\bversion\s*[:=]\s*['\"]([^'\"]+)['\"]", content, re.I)
        return m.group(1) if m else content.strip() if path.endswith("VERSION") else None
    if path.endswith("__init__.py"):
        m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", content)
        return m.group(1) if m else None
    return None

VERSION_PATHS = [
  "pyproject.toml",
  "package.json",
  "chart/Chart.yaml",
  "Chart.yaml",
  "setup.cfg",
  "setup.py",
  "VERSION",
  "src/netbox/__init__.py",
  "netbox/__init__.py",
]

def detect_version(owner, repo, ref):
    for p in VERSION_PATHS:
        v = read_file_version(owner, repo, p, ref)
        if v:
            return v, p
    try:
        tags = gh(f"https://api.github.com/repos/{owner}/{repo}/tags", params={"per_page": 1})
        if tags:
            return tags[0]["name"], "git tag (fallback)"
    except Exception:
        pass
    return None, None

def default_branch(owner, repo):
    try:
        r = gh(f"https://api.github.com/repos/{owner}/{repo}")
        return r.get("default_branch", "main")
    except requests.exceptions.HTTPError as e:
        # No access (403) or not found (404) → treat as restricted
        sc = getattr(e.response, "status_code", None)
        if sc in (403, 404):
            return None
        raise
    except Exception:
        return None

def get_head_sha(owner, repo, ref):
    data = gh(f"https://api.github.com/repos/{owner}/{repo}/commits/{ref}")
    return data.get("sha")

def priority(status, conclusion):
    # Lower number = higher priority (worse)
    if conclusion in ("failure","timed_out","cancelled","action_required"):
        return 0
    if status in ("in_progress","queued"):
        return 1
    if conclusion == "success":
        return 3
    return 2  # neutral/unknown

def get_recent_runs(owner, repo, pages=5):
    """Fetch up to 500 recent workflow runs (pages × 100)."""
    all_runs = []
    for page in range(1, pages + 1):
        resp = gh(
            f"https://api.github.com/repos/{owner}/{repo}/actions/runs",
            params={"per_page": 100, "page": page}
        )
        runs = resp.get("workflow_runs", [])
        if not runs:
            break
        all_runs.extend(runs)
    return all_runs

def latest_test_signals(owner, repo, ref, max_items=12):
    """
    Collect test signals across MANY recent runs:
      - push/schedule on the default branch
      - workflow_dispatch on default branch (or when branch is unspecified)
      - pull_request runs whose base == default branch, OR (fallback) when PR details are hidden
    Also include check runs on HEAD.
    """
    signals = []

    # 1) Recent workflow runs (paginate so monorepo PR tests aren't missed)
    try:
        runs = get_recent_runs(owner, repo, pages=5)
        for run in runs:
            event = (run.get("event") or "").lower()
            head_branch = run.get("head_branch")
            wf_name = (run.get("name") or "")
            wf_path = (run.get("path") or "")
            run_id = run.get("id")

            # Keep runs that affect the default branch
            targets_default = False
            if event in ("push", "schedule"):
                targets_default = (head_branch == ref)
            elif event == "workflow_dispatch":
                # manual runs sometimes omit head_branch
                targets_default = (head_branch == ref) or (head_branch is None)
            elif event == "pull_request":
                prs = run.get("pull_requests", [])
                if prs:
                    targets_default = any(((pr.get("base") or {}).get("ref") == ref) for pr in prs)
                else:
                    # Fallback: some tokens/org settings hide PR details → include PR runs
                    targets_default = True

            if not targets_default:
                continue

            # Is the WORKFLOW itself "test-like"?
            run_is_test = (
                (TEST_WORKFLOW_RE.search(wf_name) or TEST_WORKFLOW_RE.search(wf_path))
                and not (NON_TEST_HINT.search(wf_name) or NON_TEST_HINT.search(wf_path))
            )

            # Pull all jobs for this run
            try:
                jobs = gh(
                    f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
                    params={"per_page": 100}
                ).get("jobs", [])
            except Exception:
                jobs = []

            added_job = False
            for job in jobs:
                jname = job.get("name", "")
                # Keep jobs that look like tests OR any job from a test-like workflow
                if (run_is_test and not NON_TEST_JOB_HINT.search(jname)) or \
                   (TEST_JOB_RE.search(jname) and not NON_TEST_JOB_HINT.search(jname)):
                    signals.append({
                        "label": f"{wf_name} / {jname}" if wf_name else jname,
                        "status": job.get("status"),
                        "conclusion": job.get("conclusion"),
                        "html_url": job.get("html_url") or job.get("url"),
                        "updated_at": job.get("completed_at") or job.get("started_at") or run.get("updated_at"),
                        "source": "workflow:job",
                    })
                    added_job = True

            # If workflow is test-like but no jobs matched, keep workflow-level signal
            if run_is_test and not added_job:
                signals.append({
                    "label": wf_name or "Tests",
                    "status": run.get("status"),
                    "conclusion": run.get("conclusion"),
                    "html_url": run.get("html_url"),
                    "updated_at": run.get("updated_at"),
                    "source": "workflow",
                })
    except Exception:
        pass

    # 2) Check runs on HEAD (granular signals from external tools)
    try:
        sha = get_head_sha(owner, repo, ref)
        if sha:
            checks = gh(
                f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/check-runs",
                params={"per_page": 100}
            ).get("check_runs", [])
            for cr in checks:
                name = cr.get("name", "")
                if TEST_JOB_RE.search(name) and not NON_TEST_JOB_HINT.search(name):
                    signals.append({
                        "label": name,
                        "status": cr.get("status"),
                        "conclusion": cr.get("conclusion"),
                        "html_url": cr.get("html_url") or cr.get("details_url"),
                        "updated_at": cr.get("completed_at") or cr.get("started_at"),
                        "source": "checks",
                    })
    except Exception:
        pass

    # Dedup by label (keep newest)
    dedup = {}
    for s in signals:
        key = s["label"].strip().lower()
        if key not in dedup or (s.get("updated_at") or "") > (dedup[key].get("updated_at") or ""):
            dedup[key] = s
    signals = list(dedup.values())

    # Failures first, then newest
    signals.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
    signals.sort(key=lambda s: priority(s.get("status"), s.get("conclusion")))
    signals = signals[:max_items]

    overall = (
        min(signals, key=lambda s: priority(s.get("status"), s.get("conclusion")))
        if signals else
        {"status": "unknown", "conclusion": None, "html_url": None, "updated_at": None, "label": "Tests", "source": "none"}
    )
    return signals, overall

def build_cards():
    items = []
    for r in list_repos(ORG):
        repo = r["name"]
        if r.get("archived"):
            continue

        ref = default_branch(ORG, repo)
        if not ref:
            # No permission to read this repo; add a placeholder card and continue
            items.append({
                "repo": repo,
                "default_branch": "—",
                "version": "—",
                "version_source": "n/a",
                "overall": {"status": "unknown", "conclusion": None, "html_url": None, "updated_at": None, "label": "Restricted", "source": "perm"},
                "subtests": [],
                "has_tests": False,
                "restricted": True,
                "html_url": r.get("html_url"),
            })
            continue

        ver, vsrc = detect_version(ORG, repo, ref)
        subtests, overall = latest_test_signals(ORG, repo, ref, max_items=12)
        items.append({
            "repo": repo,
            "default_branch": ref,
            "version": ver or "—",
            "version_source": vsrc or "n/a",
            "overall": overall,
            "subtests": subtests,
            "has_tests": bool(subtests),
            "restricted": False,
            "html_url": r.get("html_url"),
        })

    # Order repo cards:
    # 1) Repos WITH tests, then WITHOUT tests, then RESTRICTED (no access)
    # 2) Within group: failing → in_progress → success → unknown
    # 3) Newest first
    # 4) Finally A–Z for stability
    items.sort(key=lambda it: it["repo"].lower())
    items.sort(key=lambda it: it["overall"].get("updated_at") or "", reverse=True)
    items.sort(key=lambda it: priority(it["overall"].get("status"), it["overall"].get("conclusion")))
    items.sort(key=lambda it: 2 if it.get("restricted") else (0 if it.get("has_tests") else 1))
    return items

def render(items):
    Path("dist").mkdir(parents=True, exist_ok=True)
    template = Template("""
<!doctype html>
<meta charset="utf-8" />
<title>NetBox Labs — Build Radiator</title>
<meta http-equiv="refresh" content="120">
<style>
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 2rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill,minmax(360px,1fr)); gap: 16px; }
  .card { border: 1px solid #ddd; border-radius: 12px; padding: 14px; }
  .h { display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }
  .dot { width:10px; height:10px; border-radius:50%; display:inline-block; margin-right:8px;}
  .ok { background:#22c55e } .fail { background:#ef4444 } .run { background:#f59e0b } .unk { background:#9ca3af }
  .meta { color:#666; font-size:12px }
  code { background:#f6f8fa; padding:2px 4px; border-radius:6px; }
  ul { margin: 8px 0 0 0; padding-left: 18px; }
  li { margin: 4px 0; }
  .label { font-weight: 500; }
  details.more { margin-top: 6px; }
  details.more summary { cursor: pointer; list-style: none; }
  details.more summary::-webkit-details-marker { display: none; }
</style>
<h1>NetBox Labs — Build Radiator</h1>
<p class="meta">Version on default branch + latest <strong>tests</strong> per repo. For monorepos, we show per-project test jobs/workflows when available. Auto-refreshes every 2 minutes.</p>
<div class="grid">
{% for it in items %}
  {% set c = "unk" %}
  {% if it.overall.conclusion == "success" %}{% set c="ok" %}{% elif it.overall.conclusion in ["failure","timed_out","cancelled","action_required"] %}{% set c="fail" %}{% elif it.overall.status in ["in_progress","queued"] %}{% set c="run" %}{% endif %}
  <div class="card">
    <div class="h">
      <a href="{{ it.html_url }}"><strong>{{ it.repo }}</strong></a>
      <span title="{{ it.overall.conclusion or it.overall.status }}"><span class="dot {{ c }}"></span></span>
    </div>
    <div>Version: <strong>{{ it.version }}</strong> <span class="meta">(from {{ it.version_source }})</span></div>
    <div>Branch: <code>{{ it.default_branch }}</code></div>
    {% if it.subtests and it.subtests|length > 0 %}
  <div class="meta" style="margin-top:6px;">Tests:</div>
  <ul>
    {% for s in it.subtests[:6] %}
      {% set sc = "unk" %}
      {% if s.conclusion == "success" %}{% set sc="ok" %}{% elif s.conclusion in ["failure","timed_out","cancelled","action_required"] %}{% set sc="fail" %}{% elif s.status in ["in_progress","queued"] %}{% set sc="run" %}{% endif %}
      <li>
        <span class="dot {{ sc }}"></span>
        {% if s.html_url %}<a href="{{ s.html_url }}" class="label">{{ s.label }}</a>{% else %}<span class="label">{{ s.label }}</span>{% endif %}
        <span class="meta">({{ s.source }})</span>
      </li>
    {% endfor %}
  </ul>

  {% if it.subtests|length > 6 %}
    <details class="more">
      <summary class="meta">…and {{ it.subtests|length - 6 }} more</summary>
      <ul>
        {% for s in it.subtests[6:] %}
          {% set sc = "unk" %}
          {% if s.conclusion == "success" %}{% set sc="ok" %}{% elif s.conclusion in ["failure","timed_out","cancelled","action_required"] %}{% set sc="fail" %}{% elif s.status in ["in_progress","queued"] %}{% set sc="run" %}{% endif %}
          <li>
            <span class="dot {{ sc }}"></span>
            {% if s.html_url %}<a href="{{ s.html_url }}" class="label">{{ s.label }}</a>{% else %}<span class="label">{{ s.label }}</span>{% endif %}
            <span class="meta">({{ s.source }})</span>
          </li>
        {% endfor %}
      </ul>
    </details>
  {% endif %}
{% else %}
  <div class="meta">Tests: no recent test signals on {{ it.default_branch }}</div>
{% endif %}
  </div>
{% endfor %}
</div>
""")
    Path("dist/index.html").write_text(template.render(items=items), encoding="utf-8")

if __name__ == "__main__":
    items = build_cards()
    render(items)
