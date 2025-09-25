import base64, json, os, re
from pathlib import Path
import requests
import tomllib as tomli  # Python 3.11 'tomllib'
import yaml
from jinja2 import Template
from datetime import datetime, timezone

ORG = os.environ.get("ORG","netboxlabs")
TOKEN = os.environ["GH_TOKEN"]
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}

# Workflows to specifically monitor in the platform-monorepo
MONITORED_WORKFLOWS = {
    'integration': [
        'testsuites_integration_integration.yaml',    # Integration tests
        'testsuites_integration_sanity.yaml',         # Sanity tests
        'testsuites_new_netbox_image.yaml'           # NetBox image tests
    ],
    'console_ui': [
        'generated_consoleui_automation_tests.yml',   # ConsoleUI Playwright tests
        'generated_consoleui_build_and_lint.yml',     # ConsoleUI build/lint
        'generated_consoleui_unit_tests.yml'         # ConsoleUI unit tests
    ]
}

# File paths to check for version information
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

# Heuristics: what looks like tests vs. non-test infra
TEST_WORKFLOW_RE = re.compile(r"(test|tests|pytest|unit|integration|e2e|ci|TestSuites)", re.I)
NON_TEST_HINT = re.compile(r"(doc|docs|page|pages|website|release|docker|publish|deploy|package|lint|format|codeql)", re.I)

def gh(url, params=None):
    """Make a GitHub API request with auth token."""
    try:
        r = requests.get(url, headers=HEADERS, params=params)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 403:
            # For restricted repos, log warning and return None
            repo_name = url.split("/repos/")[-1].split("/")[1] if "/repos/" in url else "unknown"
            print(f"Warning: Access denied to repo {repo_name} (403 Forbidden)")
            return None
        raise

def get_head_sha(owner, repo, ref):
    """Get the SHA of the HEAD commit."""
    data = gh(f"https://api.github.com/repos/{owner}/{repo}/commits/{ref}")
    return data.get("sha") if data else None

def priority(status, conclusion):
    """Priority order for test status (lower = higher priority/worse)."""
    if conclusion in ("failure","timed_out","cancelled","action_required"):
        return 0
    if status in ("in_progress","queued"):
        return 1
    if conclusion == "success":
        return 3
    return 2  # neutral/unknown

def default_branch(owner, repo):
    """Get the default branch of a repo."""
    r = gh(f"https://api.github.com/repos/{owner}/{repo}")
    return r.get("default_branch","main") if r else "main"

def get_workflow_runs(owner, repo, workflow_id):
    """Get the latest run for a specific workflow."""
    try:
        url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs"
        data = gh(url, params={"per_page": 1, "branch": "develop"})
        if data and data.get("workflow_runs"):
            latest_run = data["workflow_runs"][0]
            return {
                "status": latest_run["conclusion"] or latest_run["status"],
                "url": latest_run["html_url"],
                "updated_at": latest_run["updated_at"],
                "name": latest_run["name"]
            }
    except Exception as e:
        print(f"Error fetching workflow {workflow_id} for {owner}/{repo}: {e}")
    return None

def list_repos(org):
    """Return all repos visible to the token, including private org repos."""
    repos, page = [], 1
    # Org endpoint
    while True:
        data = gh(f"https://api.github.com/orgs/{org}/repos",
                  params={"per_page": 100, "page": page, "type": "all", "sort": "full_name"})
        if not data:
            # If we get None (403 forbidden), try the user endpoint
            break
        repos.extend(data)
        page += 1
    
    # If org endpoint failed or to complement it, try user endpoint
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
    
    # If we have no repos at all, something's wrong with the token
    if not repos:
        print(f"Warning: No repositories found for organization {org}. Check your GitHub token permissions.")
    
    return repos

def read_file_version(owner, repo, path, ref):
    """Extract version info from various file types."""
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

def detect_version(owner, repo, ref):
    """Try to detect version from various files."""
    try:
        # First try version files
        for p in VERSION_PATHS:
            v = read_file_version(owner, repo, p, ref)
            if v:
                return v, p
        
        # Fall back to git tags
        tags = gh(f"https://api.github.com/repos/{owner}/{repo}/tags", params={"per_page": 1})
        if tags:
            return tags[0]["name"], "git tag (fallback)"
    except Exception as e:
        print(f"Error detecting version for {owner}/{repo}: {e}")
    return None, "access denied/not found"

def get_monorepo_test_status():
    """Get status of all monitored test workflows in the platform-monorepo."""
    results = {
        "integration_tests": [],
        "console_ui_tests": []
    }
    
    for workflow_file in MONITORED_WORKFLOWS["integration"]:
        status = get_workflow_runs("netboxlabs", "platform-monorepo", workflow_file)
        if status:
            results["integration_tests"].append({
                "name": status["name"],
                "status": status["status"],
                "url": status["url"],
                "updated": status["updated_at"]
            })
    
    for workflow_file in MONITORED_WORKFLOWS["console_ui"]:
        status = get_workflow_runs("netboxlabs", "platform-monorepo", workflow_file)
        if status:
            results["console_ui_tests"].append({
                "name": status["name"],
                "status": status["status"],
                "url": status["url"],
                "updated": status["updated_at"]
            })
    
    return results

def latest_test_signals(owner, repo, ref, max_items=12):
    """
    Collect multiple test signals:
      - Latest run per 'test-like' workflow on the given branch; include job-level results if present.
      - Check runs on HEAD commit that look like tests.
    Returns (signals:list, overall:dict)
    each signal: {label, status, conclusion, html_url, updated_at, source}
    """
    signals = []

    # 1) Workflows that look like tests
    try:
        wfs = gh(f"https://api.github.com/repos/{owner}/{repo}/actions/workflows").get("workflows", [])
        for wf in wfs:
            name = (wf.get("name") or "")
            path = (wf.get("path") or "")
            if not (TEST_WORKFLOW_RE.search(name) or TEST_WORKFLOW_RE.search(path)):
                continue
            if NON_TEST_HINT.search(name) or NON_TEST_HINT.search(path):
                continue

            runs = gh(f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{wf['id']}/runs",
                      params={"branch": ref, "per_page": 1}).get("workflow_runs", [])
            if not runs:
                continue
            run = runs[0]
            run_id = run.get("id")

            # Try to get job-level signals (matrix jobs => per-project)
            try:
                jobs = gh(f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
                          params={"per_page": 100}).get("jobs", [])
            except Exception:
                jobs = []

            added_job = False
            for job in jobs:
                jname = job.get("name","")
                if TEST_WORKFLOW_RE.search(jname) and not NON_TEST_HINT.search(jname):
                    signals.append({
                        "label": jname,
                        "status": job.get("status"),
                        "conclusion": job.get("conclusion"),
                        "html_url": job.get("html_url") or job.get("url"),
                        "updated_at": job.get("completed_at") or job.get("started_at") or run.get("updated_at"),
                        "source": "workflow:job",
                    })
                    added_job = True
            # If no job matched, fall back to workflow-level run
            if not added_job:
                signals.append({
                    "label": name or "Tests",
                    "status": run.get("status"),
                    "conclusion": run.get("conclusion"),
                    "html_url": run.get("html_url"),
                    "updated_at": run.get("updated_at"),
                    "source": "workflow",
                })
    except Exception:
        pass

    # 2) Check runs on HEAD (often granular, includes third-party CI)
    try:
        sha = get_head_sha(owner, repo, ref)
        if sha:
            checks = gh(f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/check-runs",
                        params={"per_page": 100}).get("check_runs", [])
            for cr in checks:
                name = cr.get("name","")
                if TEST_WORKFLOW_RE.search(name) and not NON_TEST_HINT.search(name):
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

    # 3) Deduplicate by label, keep the most recent
    dedup = {}
    for s in signals:
        key = s["label"].strip().lower()
        if key not in dedup or (s.get("updated_at") or "") > (dedup[key].get("updated_at") or ""):
            dedup[key] = s
    signals = list(dedup.values())

    # Sort by priority (fail first) and, within the same priority, newest first
    signals.sort(key=lambda s: s.get("updated_at") or "", reverse=True)  # newest first
    signals.sort(key=lambda s: priority(s.get("status"), s.get("conclusion")))  # stable sort puts failures first
    signals = signals[:max_items]

    # Overall = worst (lowest priority value); tie-break by recency
    if signals:
        overall = min(signals, key=lambda s: (priority(s.get("status"), s.get("conclusion")), -(s.get("updated_at") is not None)))
    else:
        overall = {"status": "unknown", "conclusion": None, "html_url": None, "updated_at": None, "label": "Tests", "source": "none"}

    return signals, overall

def build_cards():
    """Build cards for all repos with their test statuses and versions."""
    items = []
    for r in list_repos(ORG):
        repo = r["name"]
        if r.get("archived"):
            continue
        ref = default_branch(ORG, repo)
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
            "html_url": r["html_url"],
        })

    # Order repo cards:
    # 1) Repos WITH tests first, then those without
    # 2) Within "has tests": failing → in_progress → success → unknown
    # 3) Newer updates first
    # 4) Finally A–Z by name (stable tie-breaker)
    items.sort(key=lambda it: it["repo"].lower())
    items.sort(key=lambda it: it["overall"].get("updated_at") or "", reverse=True)
    items.sort(key=lambda it: priority(it["overall"].get("status"), it["overall"].get("conclusion")))
    items.sort(key=lambda it: 0 if it["has_tests"] else 1)

    return items

def render_dashboard():
    """Generate the HTML dashboard."""
    # Get both platform-monorepo specific tests and all repo cards
    monorepo_tests = get_monorepo_test_status()
    repo_cards = build_cards()

    template = Template("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>NetBox Labs Build Radiator</title>
        <meta charset="utf-8">
        <meta http-equiv="refresh" content="120">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { 
                font-family: -apple-system, system-ui, BlinkMacSystemFont, "Segoe UI", Roboto; 
                margin: 2rem;
                line-height: 1.5;
                color: #24292e;
                background: #f6f8fa;
            }
            .container { 
                max-width: 1400px; 
                margin: 0 auto; 
                padding: 0 1rem;
            }
            .section { 
                margin-bottom: 2rem;
                background: white;
                border-radius: 6px;
                padding: 1rem;
                box-shadow: 0 1px 3px rgba(0,0,0,0.12);
            }
            .grid {
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
                gap: 1rem;
                margin-top: 1rem;
            }
            h1 { 
                color: #24292e;
                font-size: 2em;
                margin-bottom: 1rem;
            }
            h2 { 
                color: #586069;
                font-size: 1.5em;
                border-bottom: 2px solid #eaecef;
                padding-bottom: 0.3em;
            }
            .workflow, .repo-card { 
                padding: 1rem;
                border-radius: 6px;
                border: 1px solid #eaecef;
                transition: all 0.2s ease;
                height: 100%;
                display: flex;
                flex-direction: column;
                gap: 0.5rem;
            }
            .workflow:hover, .repo-card:hover {
                box-shadow: 0 4px 8px rgba(0,0,0,0.1);
                transform: translateY(-2px);
            }
            .test-header {
                color: #6e7681;
                font-size: 0.9em;
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }
            .test-name {
                font-size: 1.1em;
                margin: 0.25rem 0;
            }
            .test-status {
                margin: 0.5rem 0;
            }
            /* Card background colors */
            .repo-card.success { 
                background-color: #f0fff4;
                border-color: #98e3b3;
            }
            .repo-card.failure { 
                background-color: #fff5f5;
                border-color: #feb2b2;
            }
            .repo-card.pending, .repo-card.in_progress { 
                background-color: #fffaf0;
                border-color: #fbd38d;
            }
            .repo-card.skipped, .repo-card.unknown { 
                background-color: #f7fafc;
                border-color: #cbd5e0;
            }
            
            /* Workflow item colors */
            .workflow.success { 
                background-color: #dcffe4;
                border-color: #31c48d;
            }
            .workflow.failure { 
                background-color: #ffe5e5;
                border-color: #f05252;
            }
            .workflow.pending, .workflow.in_progress { 
                background-color: #feecdc;
                border-color: #ff8a4c;
            }
            .workflow.skipped, .workflow.unknown { 
                background-color: #f3f4f6;
                border-color: #9ca3af;
            }
            .timestamp { 
                color: #6a737d;
                font-size: 0.875rem;
                margin-top: 0.5rem;
            }
            a { 
                color: #0366d6;
                text-decoration: none;
            }
            a:hover { 
                text-decoration: underline;
            }
            .status-badge {
                display: inline-block;
                padding: 0.25em 0.6em;
                font-size: 0.75rem;
                font-weight: 500;
                border-radius: 12px;
                text-transform: capitalize;
            }
            .status-success { background-color: #dcffe4; color: #0a3622; }
            .status-failure { background-color: #ffe5e5; color: #3c0d0d; }
            .status-pending { background-color: #fff3dc; color: #3c2a0d; }
            .status-unknown { background-color: #f0f1f3; color: #1a202c; }
            .version-tag {
                display: inline-block;
                padding: 0.25em 0.6em;
                font-size: 0.75rem;
                font-weight: 500;
                border-radius: 12px;
                background-color: #e1e4e8;
                color: #24292e;
                margin-left: 0.5rem;
            }
            .subtest {
                margin-left: 1rem;
                font-size: 0.9em;
                padding: 0.5rem;
                border-left: 2px solid #eaecef;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🔍 NetBox Labs Build Radiator</h1>
            
            <div class="section">
                <h2>🚀 Platform Monorepo Tests</h2>
                <div class="grid">
                    {% for test in monorepo_tests.integration_tests %}
                        <div class="workflow {{ test.status }}">
                            <div class="test-header">
                                <strong>Integration Test</strong>
                            </div>
                            <div class="test-name">
                                <a href="{{ test.url }}">{{ test.name }}</a>
                            </div>
                            <div class="test-status">
                                <span class="status-badge status-{{ test.status }}">{{ test.status }}</span>
                            </div>
                            <div class="timestamp">Last updated: {{ test.updated }}</div>
                        </div>
                    {% endfor %}
                    {% for test in monorepo_tests.console_ui_tests %}
                        <div class="workflow {{ test.status }}">
                            <div class="test-header">
                                <strong>Console UI Test</strong>
                            </div>
                            <div class="test-name">
                                <a href="{{ test.url }}">{{ test.name }}</a>
                            </div>
                            <div class="test-status">
                                <span class="status-badge status-{{ test.status }}">{{ test.status }}</span>
                            </div>
                            <div class="timestamp">Last updated: {{ test.updated }}</div>
                        </div>
                    {% endfor %}
                </div>
            </div>
            
            <div class="section">
                <h2>📦 All Repositories</h2>
                <div class="grid">
                    {% for card in repo_cards %}
                        {% set overall_status = card.overall.conclusion or card.overall.status or 'unknown' %}
                        {% if overall_status == 'success' %}
                            {% set card_class = 'success' %}
                        {% elif overall_status in ['failure', 'timed_out', 'cancelled', 'action_required'] %}
                            {% set card_class = 'failure' %}
                        {% elif overall_status in ['in_progress', 'queued', 'pending'] %}
                            {% set card_class = 'pending' %}
                        {% else %}
                            {% set card_class = 'unknown' %}
                        {% endif %}
                        <div class="repo-card {{ card_class }}">
                            <div class="repo-header">
                                <strong><a href="{{ card.html_url }}">{{ card.repo }}</a></strong>
                                <span class="version-tag">{{ card.version }}</span>
                                {% if card.overall.html_url %}
                                    <a href="{{ card.overall.html_url }}" class="status-badge status-{{ overall_status }}">
                                        {{ card.overall.label }}
                                    </a>
                                {% endif %}
                            </div>
                        
                        {% if card.subtests %}
                            <div class="subtests">
                                {% for test in card.subtests %}
                                    <div class="subtest">
                                        <a href="{{ test.html_url }}">{{ test.label }}</a>
                                        <span class="status-badge status-{{ test.status or test.conclusion or 'unknown' }}">
                                            {{ test.status or test.conclusion or 'unknown' }}
                                        </span>
                                        {% if test.updated_at %}
                                            <div class="timestamp">{{ test.updated_at }}</div>
                                        {% endif %}
                                    </div>
                                {% endfor %}
                            </div>
                        {% endif %}
                    </div>
                {% endfor %}
            </div>
            
            <div class="timestamp">
                Generated at {{ generation_time }} · Auto-refreshes every 2 minutes
            </div>
        </div>
    </body>
    </html>
    """)
    
    html = template.render(
        monorepo_tests=monorepo_tests,
        repo_cards=repo_cards,
        generation_time=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    )
    
    # Create dist directory if it doesn't exist
    dist_dir = Path("dist")
    dist_dir.mkdir(parents=True, exist_ok=True)
    
    # Write index.html to the dist directory for GitHub Pages compatibility
    output_path = dist_dir / "index.html"
    output_path.write_text(html)
    print(f"Dashboard generated at {output_path.absolute()}")

if __name__ == "__main__":
    render_dashboard()
