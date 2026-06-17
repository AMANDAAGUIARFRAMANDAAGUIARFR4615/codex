#!/usr/bin/env python3
"""Fetch GitHub Actions job logs for debugging."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def auth() -> tuple[str, str]:
    remote = subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"],
        text=True,
    ).strip()
    match = re.search(r"(ghp_[^@]+)@github\.com/(.+?)\.git$", remote)
    if not match:
        raise RuntimeError("Cannot parse git remote")
    return match.group(1), match.group(2)


def api(path: str, token: str, repo: str) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "codex-agent",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def download(url: str, token: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "codex-agent",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read().decode("utf-8", errors="replace")


def main() -> None:
    run_id = sys.argv[1] if len(sys.argv) > 1 else "27664219786"
    token, repo = auth()
    jobs = api(f"/actions/runs/{run_id}/jobs", token, repo)["jobs"]
    job = jobs[0]
    print(f"JOB {job['id']} {job['name']} {job['conclusion']}")
    logs = download(job["logs_url"], token)
    lines = logs.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if "Pass Cloudflare and import cookies" in line),
        0,
    )
    for line in lines[start : start + 400]:
        print(line)


if __name__ == "__main__":
    main()
