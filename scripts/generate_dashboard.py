import base64, json, os, re
from pathlib import Path
import requests
import tomllib as tomli  # Python 3.11 'tomllib'
import yaml
from jinja2 import Template
from datetime import datetime, timezone
import packaging.version
import time

ORG = os.environ.get("ORG","netboxlabs")
TOKEN = os.environ["GH_TOKEN"]
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}

# Cache for package version lookups to avoid hitting rate limits
VERSION_CACHE = {}

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

# Cache for GitHub API responses
_gh_cache = {}

def gh(url, params=None):
    """Make a GitHub API request with auth token."""
    cache_key = f"{url}:{str(params)}"
    if cache_key in _gh_cache:
        return _gh_cache[cache_key]
    
    try:
        print(f"DEBUG: Requesting {url}")  # Debug line
        r = requests.get(url, headers=HEADERS, params=params)
        r.raise_for_status()
        data = r.json()
        _gh_cache[cache_key] = data
        return data
    except requests.exceptions.HTTPError as e:
        if e.response.status_code in (403, 404, 409):
            # For restricted, not found, or conflict repos, log warning and return None
            repo_name = url.split("/repos/")[-1].split("/")[1] if "/repos/" in url else "unknown"
            status_map = {403: "Access denied", 404: "Not found", 409: "Conflict"}
            print(f"Warning: {status_map[e.response.status_code]} for repo {repo_name} ({e.response.status_code})")
            print(f"DEBUG: Full URL that failed: {url}")  # Debug line
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
    return r.get("default_branch","main") if r else None

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

def get_latest_pypi_version(package_name):
    """Get the latest version of a package from PyPI."""
    cache_key = f"pypi:{package_name}"
    if cache_key in VERSION_CACHE:
        return VERSION_CACHE[cache_key]
    
    try:
        r = requests.get(f"https://pypi.org/pypi/{package_name}/json")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        version = data["info"]["version"]
        VERSION_CACHE[cache_key] = version
        return version
    except Exception as e:
        print(f"Error fetching PyPI version for {package_name}: {e}")
        return None

def get_latest_npm_version(package_name):
    """Get the latest version of a package from npm."""
    cache_key = f"npm:{package_name}"
    if cache_key in VERSION_CACHE:
        return VERSION_CACHE[cache_key]
    
    try:
        r = requests.get(f"https://registry.npmjs.org/{package_name}/latest")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        version = data["version"]
        VERSION_CACHE[cache_key] = version
        return version
    except Exception as e:
        print(f"Error fetching npm version for {package_name}: {e}")
        return None

def compare_versions(current, latest):
    """Compare two version strings, returns -1 if current is behind, 0 if equal, 1 if ahead."""
    if not current or not latest:
        return None
    
    try:
        current_v = packaging.version.parse(current.lstrip("^~=v"))
        latest_v = packaging.version.parse(latest.lstrip("^~=v"))
        if current_v < latest_v:
            return -1
        elif current_v > latest_v:
            return 1
        return 0
    except Exception:
        return None

def clean_repo_name(name):
    """Clean repository name to a consistent format."""
    # Remove common prefixes
    prefixes = ["netboxlabs-", "netbox-", "nbl-"]
    for prefix in prefixes:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name

def get_helm_dependencies(owner, repo, ref):
    """Extract dependencies from Helm charts."""
    dependencies = set()
    
    # Check both possible Helm chart locations
    chart_paths = [
        "chart/Chart.yaml",
        "Chart.yaml"
    ]
    
    print(f"Checking Helm dependencies for {owner}/{repo}")
    
    for chart_path in chart_paths:
        try:
            print(f"  Checking {chart_path}")
            chart_yaml = gh(f"https://api.github.com/repos/{owner}/{repo}/contents/{chart_path}", params={"ref": ref})
            if chart_yaml:
                print(f"  Found {chart_path}")
                content = yaml.safe_load(base64.b64decode(chart_yaml["content"]).decode("utf-8"))
                print(f"  Chart content: {json.dumps(content, indent=2)}")
                
                # Check dependencies section in Chart.yaml
                deps = content.get("dependencies", [])
                print(f"  Found {len(deps)} dependencies")
                
                for dep in deps:
                    repo_name = dep.get("name", "")
                    print(f"  Processing dependency: {repo_name}")
                    
                    # If it's a GitHub repository reference
                    if "repository" in dep:
                        repo_url = dep["repository"]
                        print(f"    Repository URL: {repo_url}")
                        
                        # Try to extract dependency name from various formats
                        if "oci://" in repo_url.lower() or "registry" in repo_url.lower():
                            # For OCI/registry references, use the chart name
                            clean_name = clean_repo_name(repo_name)
                            if clean_name:
                                print(f"    Adding OCI registry dependency: {clean_name}")
                                dependencies.add(clean_name)
                        elif f"github.com/{owner}/" in repo_url:
                            # For direct GitHub URLs
                            dep_repo = repo_url.split(f"github.com/{owner}/")[1].replace(".git", "")
                            clean_name = clean_repo_name(dep_repo)
                            print(f"    Adding GitHub dependency: {clean_name}")
                            dependencies.add(clean_name)
                        elif repo_name:
                            # For repos referenced by name
                            clean_name = clean_repo_name(repo_name)
                            print(f"    Adding named dependency: {clean_name}")
                            dependencies.add(clean_name)
        except Exception as e:
            print(f"Warning: Error checking Helm dependencies in {chart_path}: {e}")
    
    print(f"Found Helm dependencies: {dependencies}")
    return dependencies

def discover_repo_dependencies(owner, repo, ref):
    """Discover repository dependencies by analyzing various sources."""
    dependencies = set()

    # Never include the repo itself as a dependency
    def add_dependency(dep_repo):
        # Clean up repo name if it's a full URL or contains organization
        if "/" in dep_repo:
            dep_repo = dep_repo.split("/")[-1]
        if dep_repo.startswith(f"{owner}-"):
            dep_repo = dep_repo[len(f"{owner}-"):]
        if dep_repo != repo:
            dependencies.add(dep_repo)

    # Check Helm dependencies first as they're the most reliable source
    helm_deps = get_helm_dependencies(owner, repo, ref)
    for dep in helm_deps:
        add_dependency(dep)

    # Check submodules
    try:
        submodules = gh(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{ref}?recursive=1")
        if submodules and submodules.get("tree"):
            for item in submodules["tree"]:
                if item["path"].endswith(".gitmodules"):
                    content = gh(f"https://api.github.com/repos/{owner}/{repo}/contents/{item['path']}", params={"ref": ref})
                    if content:
                        decoded = base64.b64decode(content["content"]).decode("utf-8")
                        # Parse submodule URLs
                        for line in decoded.splitlines():
                            if "url =" in line:
                                url = line.split("=")[1].strip()
                                if f"github.com/{owner}/" in url:
                                    dep_repo = url.split(f"github.com/{owner}/")[1].replace(".git", "")
                                    dependencies.add(dep_repo)
    except Exception as e:
        print(f"Warning: Error checking submodules: {e}")

    # Check workflow files
    try:
        workflows = gh(f"https://api.github.com/repos/{owner}/{repo}/contents/.github/workflows", params={"ref": ref})
        if workflows:
            for workflow in workflows:
                content = gh(workflow["url"])
                if content:
                    decoded = base64.b64decode(content["content"]).decode("utf-8")
                    yaml_content = yaml.safe_load(decoded)
                    
                    # Check uses statements in workflows
                    def scan_uses(obj):
                        if isinstance(obj, dict):
                            for k, v in obj.items():
                                if k == "uses" and isinstance(v, str):
                                    if v.startswith(f"{owner}/"):
                                        dep_repo = v.split("/")[1].split("@")[0]
                                        add_dependency(dep_repo)
                                elif isinstance(v, (dict, list)):
                                    scan_uses(v)
                        elif isinstance(obj, list):
                            for item in obj:
                                scan_uses(item)
                    
                    scan_uses(yaml_content)
    except Exception as e:
        print(f"Warning: Error checking workflow dependencies: {e}")

    # Check package.json repository dependencies
    try:
        package_json = gh(f"https://api.github.com/repos/{owner}/{repo}/contents/package.json", params={"ref": ref})
        if package_json:
            content = json.loads(base64.b64decode(package_json["content"]).decode("utf-8"))
            deps = content.get("dependencies", {})
            dev_deps = content.get("devDependencies", {})
            
            for name, version in {**deps, **dev_deps}.items():
                if isinstance(version, str) and version.startswith(f"github:{owner}/"):
                    dep_repo = version.split(f"github:{owner}/")[1].split("#")[0]
                    add_dependency(dep_repo)
    except Exception:
        pass

    return list(dependencies)

def get_repo_dependencies(owner, repo, ref):
    """Get repository dependencies both from config and discovery."""
    dependencies = set()
    
    # Try loading from config file
    try:
        with open("repo-dependencies.yml") as f:
            config = yaml.safe_load(f)
            repo_config = config.get("repositories", {}).get(repo, {})
            dependencies.update(repo_config.get("dependencies", []))
    except Exception as e:
        print(f"Warning: Could not load repo dependencies from config: {e}")
    
    # Add discovered dependencies
    dependencies.update(discover_repo_dependencies(owner, repo, ref))
    
    return list(dependencies)

def get_dependencies(owner, repo, ref):
    """Get dependencies from package.json, pyproject.toml, requirements.txt, and cross-repo dependencies."""
    dependencies = {
        "python": [],
        "node": [],
        "repos": []
    }
    
    # Check cross-repo dependencies
    repo_deps = get_repo_dependencies(owner, repo, ref)
    for dep_repo in repo_deps:
        # Get the status of the dependent repo
        dep_branch = default_branch(owner, dep_repo)
        if dep_branch:  # Only process if we can access the repo
            dep_sha = get_head_sha(owner, dep_repo, dep_branch)
            if dep_sha:
                dependencies["repos"].append({
                    "name": dep_repo,
                    "branch": dep_branch,
                    "sha": dep_sha[:7],
                    "url": f"https://github.com/{owner}/{dep_repo}"
                })    # Check package.json
    try:
        data = gh(f"https://api.github.com/repos/{owner}/{repo}/contents/package.json", params={"ref": ref})
        if data:
            content = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
            deps = content.get("dependencies", {})
            dev_deps = content.get("devDependencies", {})
            
            for name, version in {**deps, **dev_deps}.items():
                # Clean up version string
                version = version.lstrip("^~=")
                latest = get_latest_npm_version(name)
                status = compare_versions(version, latest)
                dependencies["node"].append({
                    "name": name,
                    "current": version,
                    "latest": latest,
                    "status": status
                })
                # Rate limiting
                time.sleep(0.1)
    except Exception:
        pass

    # Check pyproject.toml
    try:
        data = gh(f"https://api.github.com/repos/{owner}/{repo}/contents/pyproject.toml", params={"ref": ref})
        if data:
            content = tomli.loads(base64.b64decode(data["content"]).decode("utf-8"))
            deps = content.get("project", {}).get("dependencies", [])
            dev_deps = content.get("project", {}).get("optional-dependencies", {}).get("dev", [])
            
            for dep in [*deps, *dev_deps]:
                # Parse requirement string (e.g., "requests>=2.25.1")
                match = re.match(r"([^<>=~!]+)(?:[<>=~!]+([^,]+))?", dep)
                if match:
                    name = match.group(1).strip()
                    version = match.group(2).strip() if match.group(2) else None
                    latest = get_latest_pypi_version(name)
                    status = compare_versions(version, latest)
                    dependencies["python"].append({
                        "name": name,
                        "current": version,
                        "latest": latest,
                        "status": status
                    })
                    # Rate limiting
                    time.sleep(0.1)
    except Exception:
        pass

    # Check requirements.txt
    try:
        data = gh(f"https://api.github.com/repos/{owner}/{repo}/contents/requirements.txt", params={"ref": ref})
        if data:
            content = base64.b64decode(data["content"]).decode("utf-8")
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    match = re.match(r"([^<>=~!]+)(?:[<>=~!]+([^,]+))?", line)
                    if match:
                        name = match.group(1).strip()
                        version = match.group(2).strip() if match.group(2) else None
                        latest = get_latest_pypi_version(name)
                        status = compare_versions(version, latest)
                        # Don't add duplicates from pyproject.toml
                        if not any(d["name"] == name for d in dependencies["python"]):
                            dependencies["python"].append({
                                "name": name,
                                "current": version,
                                "latest": latest,
                                "status": status
                            })
                            # Rate limiting
                            time.sleep(0.1)
    except Exception:
        pass

    return dependencies

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
        if r.get("archived") or repo == ".github":
            continue
        ref = default_branch(ORG, repo)
        ver, vsrc = detect_version(ORG, repo, ref)
        subtests, overall = latest_test_signals(ORG, repo, ref, max_items=12)
        dependencies = get_dependencies(ORG, repo, ref)
        
        # Count outdated dependencies
        outdated = {
            "python": len([d for d in dependencies["python"] if d["status"] == -1]),
            "node": len([d for d in dependencies["node"] if d["status"] == -1])
        }
        
        items.append({
            "repo": repo,
            "default_branch": ref,
            "version": ver or "—",
            "version_source": vsrc or "n/a",
            "overall": overall,
            "subtests": subtests,
            "has_tests": bool(subtests),
            "html_url": r["html_url"],
            "dependencies": dependencies,
            "outdated_deps": outdated
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
            
            .deps-section {
                margin-top: 0.5rem;
                font-size: 0.9em;
            }
            .deps-badge {
                display: inline-block;
                padding: 0.15em 0.4em;
                font-size: 0.75rem;
                font-weight: 500;
                border-radius: 4px;
                margin-right: 0.5rem;
            }
            .deps-ok { background-color: #dcffe4; color: #0a3622; }
            .deps-outdated { background-color: #ffe5e5; color: #3c0d0d; }
            .deps-header {
                color: #6e7681;
                font-size: 0.9em;
                margin-top: 0.5rem;
            }
            .deps-list {
                margin: 0.5rem 0;
                font-family: monaco, monospace;
                font-size: 0.85em;
            }
            .deps-item {
                display: flex;
                justify-content: space-between;
                padding: 0.1rem 0;
            }
            .deps-outdated-text {
                color: #e11d48;
            }
            .deps-current-text {
                color: #059669;
            }
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
            <div class="nav-links" style="margin: 1rem 0;">
                <a href="dependencies.html" style="color: #58a6ff; text-decoration: none;">View Dependencies Dashboard</a>
            </div>
            
            <div class="section">
                <h2>🚀 Platform Monorepo Tests</h2>
                <div class="grid">
                    {% set all_tests = [] %}
                    {% for test in monorepo_tests.integration_tests %}
                        {% set _ = all_tests.append({
                            'type': 'Integration Test',
                            'name': test.name,
                            'url': test.url,
                            'status': test.status,
                            'updated': test.updated,
                            'priority': 0 if test.status == 'failure' else (1 if test.status in ['in_progress', 'pending'] else (2 if test.status == 'success' else 3))
                        }) %}
                    {% endfor %}
                    {% for test in monorepo_tests.console_ui_tests %}
                        {% set _ = all_tests.append({
                            'type': 'Console UI Test',
                            'name': test.name,
                            'url': test.url,
                            'status': test.status,
                            'updated': test.updated,
                            'priority': 0 if test.status == 'failure' else (1 if test.status in ['in_progress', 'pending'] else (2 if test.status == 'success' else 3))
                        }) %}
                    {% endfor %}
                    {% set sorted_tests = all_tests|sort(attribute='priority') %}
                    {% for test in sorted_tests %}
                        <div class="workflow {{ test.status }}">
                            <div class="test-header">
                                <strong>{{ test.type }}</strong>
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
                        
                        {% if card.dependencies.python or card.dependencies.node or card.dependencies.repos %}
                            <div class="deps-section">
                                {% if card.dependencies.repos %}
                                    <div class="deps-header"><strong>Repository Dependencies</strong></div>
                                    <div class="deps-list">
                                        {% for dep in card.dependencies.repos %}
                                            <div class="deps-item">
                                                <a href="{{ dep.url }}" target="_blank">{{ dep.name }}</a>
                                                <span class="commit-sha">{{ dep.sha }}</span>
                                            </div>
                                        {% endfor %}
                                    </div>
                                {% endif %}
                                
                                {% if card.dependencies.python %}
                                    <div class="deps-header"><strong>Python Dependencies</strong></div>
                                    {% if card.outdated_deps.python > 0 %}
                                        <span class="deps-badge deps-outdated">{{ card.outdated_deps.python }} outdated</span>
                                    {% else %}
                                        <span class="deps-badge deps-ok">Up to date</span>
                                    {% endif %}
                                    <div class="deps-list">
                                        {% for dep in card.dependencies.python %}
                                            {% if dep.status == -1 %}
                                            <div class="deps-item">
                                                <span>{{ dep.name }}</span>
                                                <span>
                                                    <span class="deps-outdated-text">{{ dep.current }}</span> →
                                                    <span class="deps-current-text">{{ dep.latest }}</span>
                                                </span>
                                            </div>
                                            {% endif %}
                                        {% endfor %}
                                    </div>
                                {% endif %}
                                
                                {% if card.dependencies.node %}
                                    <div class="deps-header"><strong>Node Dependencies</strong></div>
                                    {% if card.outdated_deps.node > 0 %}
                                        <span class="deps-badge deps-outdated">{{ card.outdated_deps.node }} outdated</span>
                                    {% else %}
                                        <span class="deps-badge deps-ok">Up to date</span>
                                    {% endif %}
                                    <div class="deps-list">
                                        {% for dep in card.dependencies.node %}
                                            {% if dep.status == -1 %}
                                            <div class="deps-item">
                                                <span>{{ dep.name }}</span>
                                                <span>
                                                    <span class="deps-outdated-text">{{ dep.current }}</span> →
                                                    <span class="deps-current-text">{{ dep.latest }}</span>
                                                </span>
                                            </div>
                                            {% endif %}
                                        {% endfor %}
                                    </div>
                                {% endif %}
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

def render_dashboards():
    """Generate and write both dashboard HTMLs."""
    # Create dist directory if it doesn't exist
    Path("dist").mkdir(exist_ok=True)
    
    # Build repo cards
    repo_cards = build_cards()
    
    # Sort cards by test status (failures first) for main dashboard
    test_cards = sorted(repo_cards, key=lambda x: 
        min([priority(run["status"], run.get("conclusion")) 
            for run in x.get("test_runs", [])]
            if x.get("test_runs") else [3]))
    
    # Sort cards by name for dependencies dashboard
    dep_cards = sorted(repo_cards, key=lambda x: x["repo"])
    
    # Load templates
    with open("templates/dependencies.html", "r", encoding="utf-8") as f:
        deps_template = Template(f.read())
    
    with open("templates/dashboard.html", "r", encoding="utf-8") as f:
        main_template = Template(f.read())
    
    # Render main dashboard
    with open("dist/index.html", "w", encoding="utf-8") as f:
        f.write(main_template.render(cards=test_cards))
    
    # Render dependencies dashboard
    with open("dist/dependencies.html", "w", encoding="utf-8") as f:
        f.write(deps_template.render(cards=dep_cards))

if __name__ == "__main__":
    render_dashboards()
