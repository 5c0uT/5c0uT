#!/usr/bin/env python3
"""Generate all Git change counters for a GitHub profile repository.

Unlike GitHub's contributor statistics endpoint, this script clones every
accessible repository and reads commit diffs with `git log --numstat`.
It counts text additions/deletions and file-level changes, including binary
files such as .rar archives that have no meaningful line count.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

API_BASE = "https://api.github.com"
USER_AGENT = "profile-all-changes-stats"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def fmt_int(value: int) -> str:
    return f"{value:,}"


def split_csv(value: str) -> List[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r"[,\n\s]+", value) if part.strip()]


class GithubClient:
    def __init__(self, token: str) -> None:
        self.token = token

    def request(self, url_or_path: str) -> Tuple[Any, Dict[str, str]]:
        url = url_or_path if url_or_path.startswith("https://") else API_BASE + url_or_path
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": USER_AGENT,
            },
        )
        with urllib.request.urlopen(req, timeout=60) as response:
            body = response.read().decode("utf-8")
            data = json.loads(body) if body else None
            return data, {k.lower(): v for k, v in response.headers.items()}

    def get(self, path: str) -> Any:
        return self.request(path)[0]

    def get_optional(self, path: str) -> Optional[Any]:
        try:
            return self.get(path)
        except urllib.error.HTTPError as exc:
            print(f"::warning::GitHub API request failed for {path}: HTTP {exc.code}", file=sys.stderr)
            return None

    def paginate(self, path: str) -> Iterable[Any]:
        sep = "&" if "?" in path else "?"
        url = f"{API_BASE}{path}{sep}per_page=100"
        while url:
            data, headers = self.request(url)
            if isinstance(data, list):
                yield from data
            else:
                break
            url = None
            link = headers.get("link", "")
            for part in link.split(","):
                part = part.strip()
                if 'rel="next"' in part:
                    match = re.match(r"<([^>]+)>", part)
                    if match:
                        url = match.group(1)
                    break


def collect_author_emails(client: GithubClient, user: Dict[str, Any]) -> Set[str]:
    login = str(user.get("login") or "").strip()
    user_id = str(user.get("id") or "").strip()
    emails: Set[str] = set()

    if user.get("email"):
        emails.add(str(user["email"]).lower())

    # Needs the user:email scope. If missing, the script still works with public
    # email, GitHub no-reply email, and the optional AUTHOR_EMAILS secret.
    api_emails = client.get_optional("/user/emails")
    if isinstance(api_emails, list):
        for item in api_emails:
            address = str(item.get("email") or "").strip().lower()
            if address and item.get("verified", True):
                emails.add(address)

    if login:
        emails.add(f"{login}@users.noreply.github.com".lower())
    if login and user_id:
        emails.add(f"{user_id}+{login}@users.noreply.github.com".lower())

    for address in split_csv(env("AUTHOR_EMAILS")):
        emails.add(address.lower())

    return emails


def collect_repositories(client: GithubClient) -> List[Dict[str, Any]]:
    repos: Dict[str, Dict[str, Any]] = {}
    path = "/user/repos?visibility=all&affiliation=owner,collaborator,organization_member&sort=full_name&direction=asc"
    for repo in client.paginate(path):
        full_name = str(repo.get("full_name") or "")
        if full_name:
            repos[full_name.lower()] = repo

    # Optional list for public repos where you contributed but are not a collaborator,
    # or private repos that are visible to the token but not returned by /user/repos.
    for full_name in split_csv(env("EXTRA_REPOS")):
        full_name = full_name.strip().strip("/")
        if not full_name or "/" not in full_name:
            continue
        repo = client.get_optional(f"/repos/{urllib.parse.quote(full_name, safe='/')}")
        if isinstance(repo, dict) and repo.get("full_name"):
            repos[str(repo["full_name"]).lower()] = repo

    exclude = {name.lower() for name in split_csv(env("EXCLUDE_REPOS"))}
    filtered = []
    for repo in repos.values():
        name = str(repo.get("name") or "").lower()
        full_name = str(repo.get("full_name") or "").lower()
        if name in exclude or full_name in exclude:
            continue
        filtered.append(repo)
    filtered.sort(key=lambda r: str(r.get("full_name", "")).lower())
    return filtered


def run_git(args: List[str], cwd: Optional[str] = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    env_vars = os.environ.copy()
    env_vars.update({"GIT_TERMINAL_PROMPT": "0"})
    return subprocess.run(
        args,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env_vars,
        check=check,
    )


def clone_url_with_token(full_name: str, token: str) -> str:
    # urllib.quote keeps special characters in tokens from breaking the URL.
    encoded_token = urllib.parse.quote(token, safe="")
    return f"https://x-access-token:{encoded_token}@github.com/{full_name}.git"


def analyze_repo(repo: Dict[str, Any], token: str, author_emails: Set[str], root: str) -> Dict[str, Any]:
    full_name = str(repo["full_name"])
    target = os.path.join(root, re.sub(r"[^A-Za-z0-9_.-]+", "__", full_name) + ".git")
    stats = {
        "full_name": full_name,
        "private": bool(repo.get("private")),
        "commits": 0,
        "additions": 0,
        "deletions": 0,
        "text_lines_changed": 0,
        "files_changed": 0,
        "binary_files_changed": 0,
        "first_commit_at": None,
        "last_commit_at": None,
        "error": None,
    }

    try:
        run_git(["git", "clone", "--quiet", "--mirror", clone_url_with_token(full_name, token), target])
        log = run_git(
            [
                "git",
                "--git-dir",
                target,
                "log",
                "--all",
                "--numstat",
                "--format=@@COMMIT@@%x09%H%x09%ae%x09%an%x09%aI",
                "--no-renames",
            ]
        ).stdout
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or str(exc)).strip().splitlines()[-1:]
        stats["error"] = message[0] if message else "git command failed"
        return stats

    current_counted = False
    seen_commits: Set[str] = set()
    for raw_line in log.splitlines():
        line = raw_line.rstrip("\n")
        if line.startswith("@@COMMIT@@\t"):
            parts = line.split("\t", 4)
            current_counted = False
            if len(parts) >= 5:
                _, sha, author_email, _author_name, authored_at = parts
                if author_email.lower() in author_emails and sha not in seen_commits:
                    seen_commits.add(sha)
                    current_counted = True
                    stats["commits"] += 1
                    if authored_at:
                        first = stats["first_commit_at"]
                        last = stats["last_commit_at"]
                        stats["first_commit_at"] = authored_at if first is None or authored_at < first else first
                        stats["last_commit_at"] = authored_at if last is None or authored_at > last else last
            continue

        if not current_counted or not line.strip():
            continue

        columns = line.split("\t", 2)
        if len(columns) < 3:
            continue
        additions, deletions, _path = columns
        stats["files_changed"] += 1
        if additions == "-" or deletions == "-":
            stats["binary_files_changed"] += 1
            continue
        try:
            a = int(additions)
            d = int(deletions)
        except ValueError:
            continue
        stats["additions"] += a
        stats["deletions"] += d

    stats["text_lines_changed"] = int(stats["additions"]) + int(stats["deletions"])
    return stats


def svg_document(title: str, rows: List[Tuple[str, str]], subtitle: str) -> str:
    height = 70 + len(rows) * 24
    escaped_title = html.escape(title)
    escaped_subtitle = html.escape(subtitle)
    row_markup = []
    for idx, (label, value) in enumerate(rows):
        delay = idx * 120
        row_markup.append(
            f'<tr style="animation-delay: {delay}ms"><td>{html.escape(label)}</td><td>{html.escape(value)}</td></tr>'
        )
    return f'''<svg id="gh-dark-mode-only" width="420" height="{height}" xmlns="http://www.w3.org/2000/svg">
<style>
svg {{
  font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif, Apple Color Emoji, Segoe UI Emoji;
  font-size: 14px;
}}
#background {{
  width: calc(100% - 10px);
  height: calc(100% - 10px);
  fill: white;
  stroke: rgb(225, 228, 232);
  stroke-width: 1px;
  rx: 6px;
  ry: 6px;
}}
#gh-dark-mode-only:target #background {{ fill: #0d1117; stroke-width: 0.5px; }}
foreignObject {{ width: calc(100% - 42px); height: calc(100% - 42px); }}
table {{ width: 100%; border-collapse: collapse; table-layout: auto; }}
th {{ padding: 0 0 8px 0; text-align: left; font-size: 14px; font-weight: 600; color: rgb(3, 102, 214); }}
#gh-dark-mode-only:target th {{ color: #58a6ff; }}
td {{ padding: 3px 0; font-size: 12px; line-height: 18px; color: rgb(88, 96, 105); }}
td:last-child {{ text-align: right; font-weight: 600; }}
#gh-dark-mode-only:target td {{ color: #c9d1d9; }}
tr {{ transform: translateX(-200%); animation: slideIn 1.6s ease-in-out forwards; }}
.note {{ margin-top: 8px; font-size: 11px; color: rgb(110, 118, 129); }}
#gh-dark-mode-only:target .note {{ color: #8b949e; }}
@keyframes slideIn {{ to {{ transform: translateX(0); }} }}
</style>
<g>
<rect x="5" y="5" id="background" />
<foreignObject x="21" y="21" width="378" height="{height - 42}">
<div xmlns="http://www.w3.org/1999/xhtml">
<table>
<thead><tr style="transform: translateX(0);"><th colspan="2">{escaped_title}</th></tr></thead>
<tbody>
{''.join(row_markup)}
</tbody>
</table>
<div class="note">{escaped_subtitle}</div>
</div>
</foreignObject>
</g>
</svg>
'''


def patch_overview_svg(path: pathlib.Path, tracked_changes: int) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    # github-stats creates a row named "Lines of code changed". Replace that
    # value with our broader metric so binary add/delete commits also move it.
    pattern = r">Lines of code changed</td><td>[0-9,]+</td>"
    replacement = f">Tracked text/file changes</td><td>{fmt_int(tracked_changes)}</td>"
    new_text, count = re.subn(pattern, replacement, text, count=1)
    if count:
        path.write_text(new_text, encoding="utf-8")
        return True
    return False


def main() -> int:
    token = env("ACCESS_TOKEN")
    if not token:
        print("ACCESS_TOKEN is required", file=sys.stderr)
        return 1

    output_dir = pathlib.Path(env("OUTPUT_DIR", "generated"))
    output_dir.mkdir(parents=True, exist_ok=True)

    client = GithubClient(token)
    user = client.get("/user")
    login = str(user.get("login") or "GitHub user")
    author_emails = collect_author_emails(client, user)
    if not author_emails:
        print("::warning::No author emails found. Add the user:email token scope or AUTHOR_EMAILS secret.", file=sys.stderr)

    print(f"Counting git changes for {login}; matching {len(author_emails)} author email(s).")
    repos = collect_repositories(client)
    print(f"Repositories visible to token: {len(repos)}")

    repo_stats: List[Dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="all-git-changes-") as tmp:
        for index, repo in enumerate(repos, start=1):
            full_name = repo.get("full_name")
            print(f"[{index}/{len(repos)}] {full_name}")
            repo_stats.append(analyze_repo(repo, token, author_emails, tmp))

    errors = [r for r in repo_stats if r.get("error")]
    counted = [r for r in repo_stats if int(r.get("commits") or 0) > 0]
    totals = {
        "repositories_scanned": len(repo_stats),
        "repositories_with_commits": len(counted),
        "private_repositories_scanned": sum(1 for r in repo_stats if r.get("private")),
        "public_repositories_scanned": sum(1 for r in repo_stats if not r.get("private")),
        "commits": sum(int(r.get("commits") or 0) for r in repo_stats),
        "additions": sum(int(r.get("additions") or 0) for r in repo_stats),
        "deletions": sum(int(r.get("deletions") or 0) for r in repo_stats),
        "text_lines_changed": sum(int(r.get("text_lines_changed") or 0) for r in repo_stats),
        "files_changed": sum(int(r.get("files_changed") or 0) for r in repo_stats),
        "binary_files_changed": sum(int(r.get("binary_files_changed") or 0) for r in repo_stats),
        "repositories_with_errors": len(errors),
    }
    # This metric intentionally moves for binary-only changes: a .rar add/delete
    # increments binary_files_changed even though it has no line additions/deletions.
    totals["tracked_text_file_changes"] = totals["text_lines_changed"] + totals["binary_files_changed"]

    generated_at = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
    payload = {
        "generated_at": generated_at,
        "user": {"login": login, "id": user.get("id")},
        "matched_author_email_count": len(author_emails),
        "totals": totals,
        "repositories": repo_stats,
    }
    (output_dir / "all-changes.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    rows = [
        ("Repositories scanned", fmt_int(totals["repositories_scanned"])),
        ("Private repositories scanned", fmt_int(totals["private_repositories_scanned"])),
        ("Repositories with my commits", fmt_int(totals["repositories_with_commits"])),
        ("Commits counted", fmt_int(totals["commits"])),
        ("Text lines changed", fmt_int(totals["text_lines_changed"])),
        ("File changes", fmt_int(totals["files_changed"])),
        ("Binary file changes", fmt_int(totals["binary_files_changed"])),
        ("Tracked text/file changes", fmt_int(totals["tracked_text_file_changes"])),
    ]
    subtitle = "Counts git commit diffs across all visible refs; binary files count as file changes, not text lines."
    (output_dir / "all-changes.svg").write_text(svg_document(f"{login}'s All Git Changes", rows, subtitle), encoding="utf-8")

    patched = patch_overview_svg(output_dir / "overview.svg", int(totals["tracked_text_file_changes"]))
    if patched:
        print("Patched generated/overview.svg with tracked text/file changes.")
    else:
        print("::warning::Could not patch generated/overview.svg; all-changes.svg/json were still generated.", file=sys.stderr)

    if errors:
        print(f"::warning::{len(errors)} repositories could not be cloned/analyzed. See generated/all-changes.json.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
