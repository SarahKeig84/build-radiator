import os
import yaml
import tempfile
import json
from github import Github
from git import Repo

# --- CONFIG ---
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
ORG_NAME = "netboxlabs"
WORK_DIR = tempfile.mkdtemp()

# Get the project root directory (parent of scripts directory)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

gh = Github(GITHUB_TOKEN)
org = gh.get_organization(ORG_NAME)

chart_info = {}

print(f"Scanning repos in '{ORG_NAME}' for Helm charts...\n")

for repo in org.get_repos():
    repo_path = os.path.join(WORK_DIR, repo.name)
    try:
        repo_url = f"https://{GITHUB_TOKEN}:x-oauth-basic@github.com/{ORG_NAME}/{repo.name}.git"
        Repo.clone_from(repo_url, repo_path)

        found_chart = False
        for root, _, files in os.walk(repo_path):
            if "Chart.yaml" in files:
                found_chart = True
                chart_path = os.path.join(root, "Chart.yaml")
                values_path = os.path.join(root, "values.yaml")

                with open(chart_path, "r") as f:
                    chart_yaml = yaml.safe_load(f)

                values_yaml = {}
                if os.path.exists(values_path):
                    with open(values_path, "r") as f:
                        values_yaml = yaml.safe_load(f)

                # Extract useful fields
                chart_data = {
                    "chart_version": chart_yaml.get("version"),
                    "app_version": chart_yaml.get("appVersion"),
                    "dependencies": chart_yaml.get("dependencies", []),
                    "image_tag": values_yaml.get("image", {}).get("tag") if "image" in values_yaml else None,
                    "values_versions": {k: v for k, v in values_yaml.items() if isinstance(v, dict) and "version" in v}
                }

                chart_info[repo.name] = chart_data
                print(f"[+] Found Helm chart in {repo.name}")
                break

        if not found_chart:
            print(f"[-] No Helm chart in {repo.name}")

    except Exception as e:
        print(f"[!] Failed to process {repo.name}: {e}")

# --- Output JSON ---
output_file = os.path.join(PROJECT_ROOT, "data", "helm_chart_versions.json")
with open(output_file, "w") as f:
    json.dump(chart_info, f, indent=2)

print(f"\n[+] Scan complete. Helm chart data saved to '{output_file}'")
print(f"[+] Found {len(chart_info)} Helm charts")
