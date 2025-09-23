import base64, json, os, re
from pathlib import Path
import requests
import tomllib as tomli  # Python 3.11 'tomllib'
import yaml
from jinja2 import Template

ORG = os.environ.get("ORG","netboxlabs")
TOKEN = os.environ["GH_TOKEN"]
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}

TEST_WORKFLOW_RE = re.compile(r"(test|tests|pytest|unit|integration|ci)", re.I)
# Optional: treat these as "non-test" even if they match generic CI patterns
NON_TEST_HINT = re.compile(r"(doc|docs|page|pages|website|release|docker|publish|deploy|package|lint|format|codeql)", re.I)

def gh(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()

def list_repos(org):
    """Return all repos visible to the token, including private org repos."""
    repos, page = [], 1
    # Org endpoint (best when using classic PATs)
    while True:
        data = gh(f"https://api.github.com/orgs/{org}/repos",
                  params={"per_page": 100, "page": page, "type": "all", "sort": "full_name"})
        if not data:
            break
        repos.extend(data)
        page += 1
    # User endpoint (helps with some fine-grained PATs)
    names = {r["name"] for r in repos}
    page = 1
    while True:
        data = gh("https://api.github.com/user/repos",
                  params={"per_page": 100, "page": page, "affiliation": "organization_member"})
        if not data:
            break
        for r in data:
            if r.get("owner", {}).get("login", "").lower() == org.lower() and r["name"] not in names:
                repos.append(r)
                names.add(r["name"])
        page += 1
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
    r = gh(f"https://api.github.com/repos/{owner}/{repo}")
    return r.get("default_branch","main")

def get_head_sha(owner, repo, ref):
    data = gh(f"https://api.github.com/repos/{owner}/{repo}/commits/{ref}")
    return data.get("sha")

def latest_test_run(owner, repo, ref):
    """
    Prefer the most recent *test* workflow run on the default branch.
    Fallback: look at Check Runs on HEAD and pick one that looks like tests.
    Returns a dict with: status, conclusion, html_url, updated_at, label, source.
    """
    # 1) Look through workflows whose name/path smells like tests
    try:
        wfs = gh(f"https://api.github.com/repos/{owner}/{repo}/actions/workflows").get("workflows", [])
        candidate_ids = []
        for wf in wfs:
            name = wf.get("name") or ""
            path = wf.get("path") or ""
            if TEST_WORKFLOW_RE.search(name) or TEST_WORKFLOW_RE.search(path):
                if not NON_TEST_HINT.search(name) and not NON_TEST_HINT.search(path):
                    candidate_ids.append(wf["id"])

        best = None
        for wid in candidate_ids:
            runs = gh(f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{wid}/runs",
                      params={"branch": ref, "per_page": 1})
            wruns = runs.get("workflow_runs", [])
            if not wruns:
                continue
            run = wruns[0]
            cand = {
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "html_url": run.get("html_url"),
                "updated_at": run.get("updated_at"),
                "label": run.get("name") or "Tests",
                "source": "workflow",
            }
            if not best or (cand["updated_at"] or "") > (best["updated_at"] or ""):
                best = cand
        if best:
            return best
    except Exception:
        pass

    # 2) Fallback: check runs on HEAD commit (often include e.g. "pytest", "Unit tests")
    try:
        sha = get_head_sha(owner, repo, ref)
        if sha:
            checks = gh(f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/check-runs",
                        params={"per_page": 100})
            best = None
            for cr in checks.get("check_runs", []):
                name = cr.get("name","")
                if TEST_WORKFLOW_RE.search(name) and not NON_TEST_HINT.search(name):
                    cand = {
                        "status": cr.get("status"),
                        "conclusion": cr.get("conclusion"),
                        "html_url": cr.get("html_url") or cr.get("details_url"),
                        "updated_at": cr.get("completed_at") or cr.get("started_at"),
                        "label": name,
                        "source": "checks",
                    }
                    if not best or (cand["updated_at"] or "") > (best["updated_at"] or ""):
                        best = cand
            if best:
                return best
    except Exception:
        pass

    # 3) Nothing found
    return {"status": "unknown", "conclusion": None, "html_url": None, "updated_at": None, "label": "Tests", "source": "none"}

def build_cards():
    items = []
    for r in list_repos(ORG):
        repo = r["name"]
        if r.get("archived"):
            continue
        ref = default_branch(ORG, repo)
        ver, source = detect_version(ORG, repo, ref)
        tstatus = latest_test_run(ORG, repo, ref)
        items.append({
            "repo": repo,
            "default_branch": ref,
            "version": ver or "—",
            "version_source": source or "n/a",
            "status": tstatus,  # now reflects TESTS
            "html_url": r["html_url"],
        })
    return sorted(items, key=lambda x: x["repo"].lower())

def render(items):
    Path("dist").mkdir(parents=True, exist_ok=True)
    template = Template("""
<!doctype html>
<meta charset="utf-8" />
<title>NetBox Labs — Build Radiator</title>
<meta http-equiv="refresh" content="120">
<style>
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 2rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill,minmax(320px,1fr)); gap: 16px; }
  .card { border: 1px solid #ddd; border-radius: 12px; padding: 14px; }
  .h { display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }
  .dot { width:10px; height:10px; border-radius:50%; display:inline-block; margin-right:8px;}
  .ok { background:#22c55e } .fail { background:#ef4444 } .run { background:#f59e0b } .unk { background:#9ca3af }
  .meta { color:#666; font-size:12px }
  code { background:#f6f8fa; padding:2px 4px; border-radius:6px; }
</style>
<h1>NetBox Labs — Build Radiator</h1>
<p class="meta">Version on default branch + latest <strong>test</strong> result per repo (auto-refreshes every 2 minutes).</p>
<div class="grid">
{% for it in items %}
  {% set c = "unk" %}
  {% if it.status.conclusion == "success" %}{% set c="ok" %}{% elif it.status.conclusion in ["failure","timed_out","cancelled","action_required"] %}{% set c="fail" %}{% elif it.status.status in ["in_progress","queued"] %}{% set c="run" %}{% endif %}
  <div class="card">
    <div class="h">
      <a href="{{ it.html_url }}"><strong>{{ it.repo }}</strong></a>
      <span title="{{ it.status.conclusion or it.status.status }}"><span class="dot {{ c }}"></span></span>
    </div>
    <div>Version: <strong>{{ it.version }}</strong> <span class="meta">(from {{ it.version_source }})</span></div>
    <div>Branch: <code>{{ it.default_branch }}</code></div>
    {% if it.status.html_url %}
      <div>Tests: <a href="{{ it.status.html_url }}">{{ it.status.label }}</a> <span class="meta">({{ it.status.source }})</span></div>
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
