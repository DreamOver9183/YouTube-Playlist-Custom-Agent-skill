"""
close_redundant_prs.py — Helper script to batch close redundant Sentinel PRs.

Usage:
    python scripts/close_redundant_prs.py --token YOUR_GITHUB_TOKEN [--dry-run]
"""

import argparse
import json
import os
import sys
import urllib.request

REPO = "DreamOver9183/YouTube-Playlist-Custom-Agent-skill"
REDUNDANT_PRS = [
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
    11, 12, 13, 14, 15, 16, 18, 19, 20, 21,
    22, 23, 24, 25, 26, 27, 28, 29, 30
]

COMMENT_BODY = (
    "This PR has been consolidated and addressed directly in the `main` branch "
    "with comprehensive security fixes (Path traversal sanitization & OAuth token "
    "permission hardening). Thank you for the contribution!"
)


def main():
    parser = argparse.ArgumentParser(description="Batch close redundant PRs on GitHub")
    parser.add_argument("--token", required=False, help="GitHub Personal Access Token (or set GITHUB_TOKEN env)")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without closing PRs")
    args = parser.parse_args()

    token = args.token or os.environ.get("GITHUB_TOKEN")
    if not token and not args.dry_run:
        print("Error: GitHub Token is required to close PRs. Pass --token or set GITHUB_TOKEN, or use --dry-run.")
        sys.exit(1)

    print(f"Total redundant PRs to process: {len(REDUNDANT_PRS)}")

    for pr_num in REDUNDANT_PRS:
        print(f"Processing PR #{pr_num}...")
        if args.dry_run:
            print(f"  [DRY-RUN] Would add comment and close PR #{pr_num}")
            continue

        # 1. Post comment
        comment_url = f"https://api.github.com/repos/{REPO}/issues/{pr_num}/comments"
        req_comment = urllib.request.Request(
            comment_url,
            data=json.dumps({"body": COMMENT_BODY}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "PR-Cleanup-Bot"
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_comment) as resp:
                print(f"  Added comment to PR #{pr_num}")
        except Exception as e:
            print(f"  Warning: Failed to comment on PR #{pr_num}: {e}")

        # 2. Close PR
        pr_url = f"https://api.github.com/repos/{REPO}/pulls/{pr_num}"
        req_close = urllib.request.Request(
            pr_url,
            data=json.dumps({"state": "closed"}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "PR-Cleanup-Bot"
            },
            method="PATCH"
        )
        try:
            with urllib.request.urlopen(req_close) as resp:
                print(f"  Closed PR #{pr_num}")
        except Exception as e:
            print(f"  Error: Failed to close PR #{pr_num}: {e}")

    print("Done!")


if __name__ == "__main__":
    main()
