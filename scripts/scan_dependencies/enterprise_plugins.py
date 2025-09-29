import os
import re
import json
import sys
import requests
import base64
import yaml
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

TARGET_REPO = "netbox-enterprise"

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

# ---- Patterns to detect NetBox version references ----
version_patterns = [
    re.compile(r'netbox\s*=?\s*[\'"]?([0-9]+\.[0-9]+\.[0-9]+)[\'"]?', re.IGNORECASE),
    re.compile(r'min_version:\s*[\'"]?([0-9]+\.[0-9]+\.[0-9]+)[\'"]?'),
    re.compile(r'max_version:\s*[\'"]?([0-9]+\.[0-9]+\.[0-9]+)[\'"]?'),
]

def process_file_content(file_name, content):
    """Process file content to extract version information."""
    versions = []
    if file_name in ["plugin.yaml", "plugin.yml"]:
        try:
            data = yaml.safe_load(content)
            for key in ["min_version", "max_version"]:
                if key in data:
                    versions.append(f"{key}: {data[key]}")
        except Exception:
            pass
    elif file_name in ["pyproject.toml", "setup.py", "requirements.txt", "README.md"]:
        for pattern in version_patterns:
            matches = pattern.findall(content)
            if matches:
                versions.extend(matches)
    return versions

def main():
    """Main function to scan enterprise plugins."""
    # Create data directory if it doesn't exist
    Path("data").mkdir(exist_ok=True)
    
    print(f"Scanning {TARGET_REPO} repository...")
    
    # Get repository contents recursively
    plugin_metadata = []
    contents = get_repo_contents(ORG, TARGET_REPO)
    
    def scan_directory(path=""):
        items = get_repo_contents(ORG, TARGET_REPO, path)
        if not items:
            return
            
        for item in items:
            if item["type"] == "dir":
                scan_directory(item["path"])
                continue
                
            file_name = item["name"]
            if file_name in ["plugin.yaml", "plugin.yml", "pyproject.toml", "setup.py", "requirements.txt", "README.md"]:
                content = get_file_content(item)
                if not content:
                    continue
                    
                versions = process_file_content(file_name, content)
                if versions:
                    plugin_info = {
                        "file": item["path"],
                        "plugin_name": os.path.basename(os.path.dirname(item["path"])),
                        "netbox_versions": versions
                    }
                    plugin_metadata.append(plugin_info)
    
    scan_directory()
    
    # Save results
    output_file = "data/enterprise_plugins_report.json"
    with open(output_file, "w") as f:
        json.dump(plugin_metadata, f, indent=2)
    
    print(f"✅ Done. Found {len(plugin_metadata)} plugins with version information")
    print(f"Report saved to {output_file}")

if __name__ == "__main__":
    main()
