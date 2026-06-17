#!/usr/bin/env python3
"""触发 GitHub Actions 上的 "Serve Claude (frp)" 工作流。

用法:
    python trigger_actions.py [分钟数]        # 默认 30
    python trigger_actions.py --logs          # 打印最近一次运行的日志（排错）

触发后不会长时间阻塞：打印运行 URL 与「如何从控制台连上提问」的命令。
服务在 runner 上登录 claude.ai 并通过 frp 暴露，登录约需 2-3 分钟。
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
LOG_STEP_MARKER = "Serve Claude over frp"
DEFAULT_MINUTES = "30"


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
    start = next((i for i, line in enumerate(lines) if LOG_STEP_MARKER in line), 0)
    print("\n".join(lines[start:]))


def frp_endpoint() -> tuple[str, str]:
    """从 frpc.toml 读出 serverAddr 和 remotePort，用于拼连接命令。"""
    text = (REPO_ROOT / "frpc.toml").read_text(encoding="utf-8")
    host = re.search(r'serverAddr\s*=\s*"([^"]+)"', text)
    port = re.search(r"remotePort\s*=\s*(\d+)", text)
    return (host.group(1) if host else "<frps_ip>", port.group(1) if port else "<remotePort>")


def tail_latest_logs() -> None:
    token, repo = auth()
    run = api("GET", "/actions/runs?per_page=1", token, repo)["workflow_runs"][0]
    print(f"Run {run['id']}: {run['html_url']} ({run['status']}/{run.get('conclusion')})")
    print_job_logs(run["id"], token, repo)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--logs":
        tail_latest_logs()
        return

    minutes = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SERVE_MINUTES", DEFAULT_MINUTES)
    token, repo = auth()
    workflow = next(
        item
        for item in api("GET", "/actions/workflows", token, repo)["workflows"]
        if item["path"].endswith(WORKFLOW_FILE)
    )
    print(f"触发服务（存活 {minutes} 分钟）...")
    dispatch = api(
        "POST",
        f"/actions/workflows/{workflow['id']}/dispatches",
        token,
        repo,
        {"ref": "main", "inputs": {"minutes": str(minutes), "cookie_file": "cookie.json"}},
    )
    if dispatch:
        print(json.dumps(dispatch, indent=2))

    print("已触发，等待运行出现...")
    time.sleep(8)
    run = api("GET", "/actions/runs?per_page=1", token, repo)["workflow_runs"][0]
    print(f"Run {run['id']}: {run['html_url']}")

    host, port = frp_endpoint()
    print("\n登录约需 2-3 分钟。就绪后在你的控制台运行（流式实时输出）：")
    print(f'  curl -N "http://{host}:{port}/ask?q=你的问题"')
    print(f'  curl -N "http://{host}:{port}/new"      # 开启新对话（清空上下文）')
    print(f'  curl    "http://{host}:{port}/health"   # 健康检查')
    print(f"\n查看运行日志/排错: python {Path(__file__).name} --logs")


if __name__ == "__main__":
    main()
