import os
import re
import tempfile
import json
from github import Github
from git import Repo

# ---- Configuration ----
ORG_NAME = "netboxlabs"
UPSTREAM_VERSION_PATTERNS = {
    "netbox": re.compile(r"netbox.*?(v?4\.2\.9)", re.IGNORECASE),
    "redis": re.compile(r"redis.*?(7\.4\.2)", re.IGNORECASE),
    "postgresql": re.compile(r"postgres.*?(16\.8)", re.IGNORECASE),
    "diode": re.compile(r"diode.*?(1\.2\.0)", re.IGNORECASE)
}
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- Get GitHub token from environment ----
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
if not GITHUB_TOKEN:
    raise EnvironmentError("Missing GITHUB_TOKEN. Run: export GITHUB_TOKEN=your_token")

# ---- Initialize GitHub API client ----
gh = Github(GITHUB_TOKEN)
org = gh.get_organization(ORG_NAME)
repos = org.get_repos()

# ---- Temporary workspace ----
WORK_DIR = tempfile.mkdtemp()
print(f"🔍 Cloning repos to {WORK_DIR}\n")

# ---- Scan logic ----
def scan_upstream_versions(repo_path):
    found = {}
    for root, _, files in os.walk(repo_path):
        for file in files:
            if file.endswith(('.yaml', '.yml', '.Dockerfile', '.txt', '.py', '.sh', 'Dockerfile')):
                full_path = os.path.join(root, file)
                try:
                    with open(full_path, "r", encoding="utf-8") as f:
                        content = f.read()
                        for dep, pattern in UPSTREAM_VERSION_PATTERNS.items():
                            if pattern.search(content):
                                found[dep] = pattern.search(content).group(0)
                except Exception:
                    continue
    return found

# ---- Process each repo ----
results = {}
for repo in repos:
    try:
        print(f"→ Cloning {repo.name}")
        path = os.path.join(WORK_DIR, repo.name)
        Repo.clone_from(repo.clone_url.replace("https://", f"https://{GITHUB_TOKEN}:x-oauth-basic@"), path)

        versions = scan_upstream_versions(path)
        if versions:
            results[repo.name] = versions
            print(f"✓ Found: {versions}")
        else:
            print("– No matches")

    except Exception as e:
        print(f"⚠️ Skipping {repo.name}: {e}")

# ---- Output results ----
print("\n✅ Scan complete.\n")

output_file = os.path.join(PROJECT_ROOT, "data", "scan_upstream_versions.json")
with open(output_file, "w") as f:
    json.dump(results, f, indent=2)

print(f"📄 Results saved to: {output_file}")
