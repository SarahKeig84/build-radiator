# Dependency Scanner

This module scans GitHub repositories within an organization to detect cross-repository references and dependencies.

## Requirements

- Python 3.6+
- GitHub Personal Access Token with repo access

## Environment Variables

- `GH_TOKEN`: GitHub Personal Access Token (required)
- `ORG`: GitHub organization name (default: "netboxlabs")

## Usage

```bash
# Set your GitHub token
$env:GH_TOKEN="your_token_here"  # Windows PowerShell
# export GH_TOKEN="your_token_here"  # Linux/macOS

# Optional: Set organization
$env:ORG="your_org_name"

# Run the scanner
python cross_repo_mentions.py
```

## Output

The script generates a JSON file at `data/cross_repo_mentions.json` with the following structure:

```json
{
  "repo-name": [
    "referenced-repo-1",
    "referenced-repo-2"
  ]
}
```

This data is used by the build radiator to generate the dependencies view in the dashboard.
