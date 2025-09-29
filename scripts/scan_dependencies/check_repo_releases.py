import os
import json
import sys
import time
import requests
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

def main():
    """Main function to check repository releases and tags."""
    # Create data directory if it doesn't exist
    Path("data").mkdir(exist_ok=True)

    release_data = {}
        # Get all repositories in the organization
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

    release_data = {}
    
    # Check each repository
    for repo_data in repos:
        repo_name = repo_data["name"]
        repo_info = {"latest_release": None, "latest_tag": None}
        
        # Try GitHub Releases first
        releases_url = f"https://api.github.com/repos/{ORG}/{repo_name}/releases"
        releases = gh(releases_url)
        
        if releases and len(releases) > 0:
            latest = releases[0]
            repo_info["latest_release"] = {
                "name": latest.get("name") or latest.get("tag_name"),
                "tag": latest.get("tag_name"),
                "published_at": latest.get("published_at"),
                "url": latest.get("html_url")
            }
        
        # Fallback: Check tags
        tags_url = f"https://api.github.com/repos/{ORG}/{repo_name}/tags"
        tags = gh(tags_url)
        
        if tags and len(tags) > 0:
            latest_tag = tags[0]
            repo_info["latest_tag"] = {
                "name": latest_tag.get("name"),
                "url": f"https://github.com/{ORG}/{repo_name}/tree/{latest_tag.get('name')}"
            }
        
        release_data[repo_name] = repo_info
        print(f"{repo_name:<30} → "
              f"Release: {repo_info['latest_release']['tag'] if repo_info['latest_release'] else '–'} | "
              f"Tag: {repo_info['latest_tag']['name'] if repo_info['latest_tag'] else '–'}")
    
    # Save results to both files for compatibility
    output_files = [
        "data/repo_release_summary.json",
        "data/check_repo_releases.json"
    ]
    
    for output_file in output_files:
        with open(output_file, "w") as f:
            json.dump(release_data, f, indent=2)
    
    print("\n✅ Done. Release summary saved")
    
    # Load previous state and diff
    if os.path.exists("prev_releases.json"):
        with open("prev_releases.json") as f:
            old = json.load(f)
        
        for repo, current in release_data.items():
            old_release = old.get(repo, {}).get("latest_release", {}).get("tag")
            new_release = current.get("latest_release", {}).get("tag")
            if old_release != new_release:
                print(f"🚨 New release detected in {repo}: {old_release} → {new_release}")

if __name__ == "__main__":
    main()
