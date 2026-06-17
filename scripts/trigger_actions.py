#!/usr/bin/env python3
"""Trigger GitHub Actions workflow and poll until completion.

用法:
    python trigger_actions.py "你的问题"
不传则用默认问题（或环境变量 CLAUDE_PROMPT）。运行结束后打印任务日志，
并高亮 claude.ai 的回答（CLAUDE ANSWER 标记之间的内容）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_FILE = "import-claude-cookie.yml"
LOG_STEP_MARKER = "Login and ask Claude"
DEFAULT_PROMPT = "用一句话介绍你自己。"


def auth() -> tuple[str, str]:
    remote = subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"],
        text=True,
    ).strip()
    match = re.search(r"(ghp_[^@]+)@github\.com/(.+?)\.git$", remote)
    if not match:
        raise RuntimeError("Cannot parse git remote")
    return match.group(1), match.group(2)


def api(method: str, path: str, token: str, repo: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "codex-agent",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode()
        return json.loads(body) if body else {}


def download(url: str, token: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "codex-agent",
        },
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirect())
    try:
        opener.open(req)
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location")
        if not location:
            raise
        with urllib.request.urlopen(location, timeout=120) as resp:
            return resp.read().decode("utf-8", errors="replace")
    raise RuntimeError("Unexpected logs response")


def print_job_logs(run_id: int, token: str, repo: str) -> None:
    jobs = api("GET", f"/actions/runs/{run_id}/jobs", token, repo)["jobs"]
    job = jobs[0]
    logs = download(f"https://api.github.com/repos/{repo}/actions/jobs/{job['id']}/logs", token)
    lines = logs.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if LOG_STEP_MARKER in line),
        0,
    )
    print("\n".join(lines[start:]))
    print_answer(lines)


def print_answer(lines: list[str]) -> None:
    """从日志里抽出 CLAUDE ANSWER 标记之间的回答并高亮打印。"""
    begin = next((i for i, line in enumerate(lines) if "CLAUDE ANSWER BEGIN" in line), None)
    end = next((i for i, line in enumerate(lines) if "CLAUDE ANSWER END" in line), None)
    if begin is None or end is None or end <= begin:
        return
    # 去掉每行前面的 GitHub 时间戳前缀。
    answer = [re.sub(r"^\S+\s", "", ln) for ln in lines[begin + 1 : end]]
    print("\n========== CLAUDE 回答 ==========")
    print("\n".join(answer))
    print("=================================\n")


def main() -> None:
    prompt = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CLAUDE_PROMPT", DEFAULT_PROMPT)
    token, repo = auth()
    workflow = next(
        item
        for item in api("GET", "/actions/workflows", token, repo)["workflows"]
        if item["path"].endswith(WORKFLOW_FILE)
    )
    print(f"提问: {prompt!r}")
    dispatch = api(
        "POST",
        f"/actions/workflows/{workflow['id']}/dispatches",
        token,
        repo,
        {"ref": "main", "inputs": {"prompt": prompt, "cookie_file": "cookie.json"}},
    )
    if dispatch:
        print(json.dumps(dispatch, indent=2))

    print("Workflow dispatched, waiting for run...")
    time.sleep(8)
    run = api("GET", "/actions/runs?per_page=1", token, repo)["workflow_runs"][0]
    run_id = run["id"]
    print(f"Run {run_id}: {run['html_url']}")

    while True:
        run = api("GET", f"/actions/runs/{run_id}", token, repo)
        status = run["status"]
        conclusion = run.get("conclusion")
        print(f"status={status} conclusion={conclusion}")
        if status == "completed":
            print_job_logs(run_id, token, repo)
            if conclusion != "success":
                sys.exit(1)
            print("Workflow succeeded")
            return
        time.sleep(20)


if __name__ == "__main__":
    main()
