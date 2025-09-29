import os
import yaml
import json
import sys
import requests
import base64
from pathlib import Path

# Configuration
try:
    # Try different token environment variables used in GitHub Actions
    TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not TOKEN:
        raise KeyError("No GitHub token found")
    
    ORG = os.environ.get("ORG", "netboxlabs")
    HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}
except KeyError as e:
    print("Error: GitHub token not found in environment variables.")
    print("Please ensure one of these environment variables is set:")
    print("- GH_TOKEN: Personal access token")
    print("- GITHUB_TOKEN: GitHub Actions token")
    print("\nIn GitHub Actions, the token is automatically available as GITHUB_TOKEN")
    print("For local development, you need to set GH_TOKEN manually.")
    sys.exit(1)

def gh(url, params=None):
    """Make a GitHub API request with auth token."""
    try:
        r = requests.get(url, headers=HEADERS, params=params)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        if e.response.status_code in (403, 404, 409):
            repo_name = url.split("/repos/")[-1].split("/")[1] if "/repos/" in url else "unknown"
            status_map = {403: "Access denied", 404: "Not found", 409: "Conflict"}
            print(f"Warning: {status_map[e.response.status_code]} for repo {repo_name} ({e.response.status_code})")
        return None

def get_repo_contents(owner, repo, path=""):
    """Get contents of a repository path."""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    return gh(url)

def get_file_content(content_obj):
    """Get decoded content from a GitHub file object."""
    if not content_obj or not isinstance(content_obj, dict):
        return None
    if content_obj.get("type") != "file":
        return None
    if "content" not in content_obj:
        print(f"Warning: No content field in response for {content_obj.get('path', 'unknown file')}")
        return None
    try:
        return base64.b64decode(content_obj["content"]).decode("utf-8")
    except Exception as e:
        print(f"Warning: Failed to decode content for {content_obj.get('path', 'unknown file')}: {e}")
        return None

def main():
    """Main function to scan repositories for Helm charts."""
    # Create data directory if it doesn't exist
    Path("data").mkdir(exist_ok=True)
    
    # Get all repositories in the organization
    print(f"Scanning repos in '{ORG}' for Helm charts...")
    repos_url = f"https://api.github.com/orgs/{ORG}/repos"
    repos = []
    page = 1
    
    while True:
        data = gh(repos_url, params={"page": page, "per_page": 100})
        if not data:
            break
        repos.extend(data)
        if len(data) < 100:
            break
        page += 1
    
    chart_info = {}
    
    # Scan each repository
    for repo_data in repos:
        repo_name = repo_data["name"]
        try:
            # Check for Chart.yaml
            chart_yaml_content = get_file_content(get_repo_contents(ORG, repo_name, "Chart.yaml"))
            if not chart_yaml_content:
                print(f"[-] No Helm chart in {repo_name}")
                continue
                
            # Parse Chart.yaml
            chart_yaml = yaml.safe_load(chart_yaml_content)
            
            # Try to get values.yaml
            values_yaml = {}
            values_content = get_file_content(get_repo_contents(ORG, repo_name, "values.yaml"))
            if values_content:
                values_yaml = yaml.safe_load(values_content)
            
            # Extract useful fields
            chart_data = {
                "chart_version": chart_yaml.get("version"),
                "app_version": chart_yaml.get("appVersion"),
                "dependencies": chart_yaml.get("dependencies", []),
                "image_tag": values_yaml.get("image", {}).get("tag") if "image" in values_yaml else None,
                "values_versions": {k: v for k, v in values_yaml.items() if isinstance(v, dict) and "version" in v}
            }
            
            chart_info[repo_name] = chart_data
            print(f"[+] Found Helm chart in {repo_name}")
            
        except Exception as e:
            print(f"[!] Failed to process {repo_name}: {e}")
    
    # Save results
    output_file = "data/helm_chart_versions.json"
    with open(output_file, "w") as f:
        json.dump(chart_info, f, indent=2)
    
    print(f"\n✅ Done. Found {len(chart_info)} Helm charts")
    print(f"Report saved to {output_file}")

if __name__ == "__main__":
    main()
