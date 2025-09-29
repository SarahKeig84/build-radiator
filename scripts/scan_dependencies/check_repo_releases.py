import os
import json
from github import Github
from datetime import datetime

# --- CONFIG ---
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
ORG_NAME = "netboxlabs"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- INIT ---
gh = Github(GITHUB_TOKEN)
org = gh.get_organization(ORG_NAME)

release_data = {}

print(f"Checking latest releases/tags for repos in '{ORG_NAME}'...\n")

for repo in org.get_repos():
    repo_info = {"latest_release": None, "latest_tag": None}

    # Try GitHub Releases first
    try:
        releases = repo.get_releases()
        if releases.totalCount > 0:
            latest = releases[0]
            repo_info["latest_release"] = {
                "name": latest.title or latest.tag_name,
                "tag": latest.tag_name,
                "published_at": latest.published_at.strftime('%Y-%m-%d %H:%M:%S'),
                "url": latest.html_url
            }
    except Exception:
        pass

    # Fallback: Check tags
    try:
        tags = repo.get_tags()
        if tags.totalCount > 0:
            latest_tag = tags[0]
            repo_info["latest_tag"] = {
                "name": latest_tag.name,
                "url": f"https://github.com/{ORG_NAME}/{repo.name}/tree/{latest_tag.name}"
            }
    except Exception:
        pass

    release_data[repo.name] = repo_info
    print(f"{repo.name:<30} → "
          f"Release: {repo_info['latest_release']['tag'] if repo_info['latest_release'] else '–'} | "
          f"Tag: {repo_info['latest_tag']['name'] if repo_info['latest_tag'] else '–'}")

# --- Save results ---
with open("repo_release_summary.json", "w") as f:
    json.dump(release_data, f, indent=2)

# --- Output JSON ---
output_file = os.path.join(PROJECT_ROOT, "data", "check_repo_releases.json")
with open(output_file, "w") as f:
    json.dump(release_data, f, indent=2)

print("\n✅ Done. Release summary saved to 'repo_release_summary.json'")

# Load previous state and diff
if os.path.exists("prev_releases.json"):
    with open("prev_releases.json") as f:
        old = json.load(f)

    for repo, current in release_data.items():
        old_release = old.get(repo, {}).get("latest_release", {}).get("tag")
        new_release = current.get("latest_release", {}).get("tag")
        if old_release != new_release:
            print(f"🚨 New release detected in {repo}: {old_release} → {new_release}")
