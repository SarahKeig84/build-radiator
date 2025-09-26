import os
import re
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
MENTION_PATTERN_TEMPLATE = r"(netboxlabs/{repo})"

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
    if content_obj.get("type") != "file":
        return None
    return base64.b64decode(content_obj["content"]).decode("utf-8")

def scan_repo_for_mentions(owner, repo, all_repos):
    """Scan a repository for mentions of other repositories."""
    mentions = set()
    
    # Get repository tree
    contents = get_repo_contents(owner, repo)
    if not contents:
        return mentions
        
    def process_item(item):
        if isinstance(item, list):
            for i in item:
                process_item(i)
            return
            
        if item.get("type") == "dir":
            dir_contents = get_repo_contents(owner, repo, item["path"])
            process_item(dir_contents)
            return
            
        if item.get("type") == "file":
            # Skip binary files
            if item["name"].endswith(('.pyc', '.bin', '.png', '.jpg', '.pdf', '.zip')):
                return
                
            content = get_file_content(item)
            if not content:
                return
                
            # Check for mentions of other repos
            for target_repo in all_repos:
                if target_repo == repo:
                    continue
                pattern = re.compile(MENTION_PATTERN_TEMPLATE.format(repo=re.escape(target_repo)))
                if pattern.search(content):
                    mentions.add(target_repo)
    
    process_item(contents)
    return mentions

def main():
    """Main function to scan repositories for cross-references."""
    # Create data directory if it doesn't exist
    Path("data").mkdir(exist_ok=True)
    
    # Get all repositories in the organization
    print(f"Fetching repos from {ORG}...")
    repos_url = f"https://api.github.com/orgs/{ORG}/repos"
    repos = []
    page = 1
    
    while True:
        data = gh(repos_url, params={"page": page, "per_page": 100})
        if not data:
            break
        repos.extend([r["name"] for r in data])
        if len(data) < 100:
            break
        page += 1
    
    print(f"Found {len(repos)} repositories")
    repo_mentions = {}
    
    # Scan each repository
    for repo in repos:
        print(f"Scanning {repo}...")
        mentions = scan_repo_for_mentions(ORG, repo, repos)
        if mentions:
            repo_mentions[repo] = sorted(list(mentions))
            print(f"✓ {repo} references: {', '.join(mentions)}")
        else:
            print(f"- No mentions found in {repo}")
    
    # Save results
    output_file = "data/cross_repo_mentions.json"
    with open(output_file, "w") as f:
        json.dump(repo_mentions, f, indent=2)
    
    print(f"\n✅ Done. Report saved to {output_file}")

if __name__ == "__main__":
    main()
