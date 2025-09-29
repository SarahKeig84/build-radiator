import os
import re
import json
import sys
import time
import requests
import base64
from datetime import datetime
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

# Version patterns to search for
UPSTREAM_VERSION_PATTERNS = {
    "netbox": re.compile(r"netbox.*?(v?4\.2\.9)", re.IGNORECASE),
    "redis": re.compile(r"redis.*?(7\.4\.2)", re.IGNORECASE),
    "postgresql": re.compile(r"postgres.*?(16\.8)", re.IGNORECASE),
    "diode": re.compile(r"diode.*?(1\.2\.0)", re.IGNORECASE)
}

def check_rate_limit():
    """Check GitHub API rate limit status."""
    try:
        r = requests.get("https://api.github.com/rate_limit", headers=HEADERS)
        r.raise_for_status()
        data = r.json()
        core = data["resources"]["core"]
        remaining = core["remaining"]
        reset_time = datetime.fromtimestamp(core["reset"]).strftime('%Y-%m-%d %H:%M:%S')
        
        if remaining < 100:  # Warning threshold
            print(f"\n⚠️  GitHub API rate limit low: {remaining} requests remaining")
            print(f"Rate limit will reset at {reset_time}")
            
        if remaining < 10:  # Critical threshold
            print("Rate limit too low to continue safely.")
            print("Waiting for rate limit reset...")
            time.sleep(max(0, core["reset"] - time.time() + 1))
            
        return remaining
    except Exception as e:
        print(f"Warning: Could not check rate limit: {e}")
        return None

def gh(url, params=None):
    """Make a GitHub API request with auth token."""
    try:
        # Check rate limit before making request
        remaining = check_rate_limit()
        if remaining is not None and remaining < 10:
            print("Rate limit critically low, request aborted")
            return None
            
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

def scan_file_content(content, file_name):
    """Scan file content for upstream version mentions."""
    if not content:
        return {}
        
    found = {}
    if file_name.endswith(('.yaml', '.yml', '.Dockerfile', '.txt', '.py', '.sh', 'Dockerfile')):
        for dep, pattern in UPSTREAM_VERSION_PATTERNS.items():
            if pattern.search(content):
                found[dep] = pattern.search(content).group(0)
    return found

def main():
    """Main function to scan repositories for upstream version mentions."""
    # Create data directory if it doesn't exist
    Path("data").mkdir(exist_ok=True)
    
    # Get all repositories in the organization
    print(f"Scanning repos in '{ORG}' for upstream version mentions...")
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
    
    results = {}
    
    # Process each repository
    for repo_data in repos:
        repo_name = repo_data["name"]
        print(f"→ Scanning {repo_name}")
        
        try:
            def scan_directory(path=""):
                items = get_repo_contents(ORG, repo_name, path)
                if not items:
                    return {}
                
                found_versions = {}
                
                for item in items:
                    if item["type"] == "dir":
                        # Recursively scan subdirectories
                        sub_versions = scan_directory(item["path"])
                        found_versions.update(sub_versions)
                    elif item["type"] == "file":
                        file_name = item["name"]
                        if file_name.endswith(('.yaml', '.yml', '.Dockerfile', '.txt', '.py', '.sh', 'Dockerfile')):
                            content = get_file_content(item)
                            versions = scan_file_content(content, file_name)
                            found_versions.update(versions)
                
                return found_versions
            
            versions = scan_directory()
            if versions:
                results[repo_name] = versions
                print(f"✓ Found: {versions}")
            else:
                print("– No matches")
                
        except Exception as e:
            print(f"⚠️ Skipping {repo_name}: {e}")
    
    # Save results
    output_file = "data/upstream_version_report.json"
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    
    print(f"\n✅ Done. Scanned {len(repos)} repositories")
    print(f"Found upstream version mentions in {len(results)} repositories")
    print(f"Report saved to {output_file}")

if __name__ == "__main__":
    main()
