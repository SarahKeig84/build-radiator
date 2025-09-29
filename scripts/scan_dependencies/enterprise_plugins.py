import os
import re
import tempfile
from git import Repo
from github import Github
import yaml
import json

# --- CONFIGURATION ---
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
ORG_NAME = "netboxlabs"
WORK_DIR = tempfile.mkdtemp()
PLUGIN_METADATA = []
TARGET_REPO = "netbox-enterprise"

# Get the project root directory (parent of scripts directory)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

# --- INIT ---
gh = Github(GITHUB_TOKEN)
org = gh.get_organization(ORG_NAME)

# --- Get all repo names in org ---
print(f"Fetching all repos from org '{ORG_NAME}'...")
repo_names = [r.name for r in org.get_repos()]

# ---- Patterns to detect NetBox version references ----
version_patterns = [
    re.compile(r'netbox\s*=?\s*[\'"]?([0-9]+\.[0-9]+\.[0-9]+)[\'"]?', re.IGNORECASE),
    re.compile(r'min_version:\s*[\'"]?([0-9]+\.[0-9]+\.[0-9]+)[\'"]?'),
    re.compile(r'max_version:\s*[\'"]?([0-9]+\.[0-9]+\.[0-9]+)[\'"]?'),
]

# ---- Walk repo and inspect relevant files ----
if TARGET_REPO in repo_names:
    repo_path = os.path.join(WORK_DIR, TARGET_REPO)
    print(f"Cloning {TARGET_REPO}...")
    Repo.clone_from(f"https://{GITHUB_TOKEN}:x-oauth-basic@github.com/{ORG_NAME}/{TARGET_REPO}.git", repo_path)

    for root, dirs, files in os.walk(repo_path):
        for file in files:
            path = os.path.join(root, file)
            plugin_info = {"file": path, "plugin_name": os.path.basename(root), "netbox_versions": []}

            if file in ["plugin.yaml", "plugin.yml"]:
                try:
                    with open(path) as f:
                        data = yaml.safe_load(f)
                    for key in ["min_version", "max_version"]:
                        if key in data:
                            plugin_info["netbox_versions"].append(f"{key}: {data[key]}")
                except Exception:
                    continue

            elif file in ["pyproject.toml", "setup.py", "requirements.txt", "README.md"]:
                try:
                    with open(path) as f:
                        content = f.read()
                    for pattern in version_patterns:
                        matches = pattern.findall(content)
                        if matches:
                            plugin_info["netbox_versions"].extend(matches)
                except Exception:
                    continue

            if plugin_info["netbox_versions"]:
                PLUGIN_METADATA.append(plugin_info)

# ---- Output results ----
output_path = os.path.join(PROJECT_ROOT, "data", "enterprise_plugins_report.json")
with open(output_path, "w") as f:
    json.dump(PLUGIN_METADATA, f, indent=2)

print(f"[+] Scan complete. Results saved to: {output_path}")
