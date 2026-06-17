# Claude 登录 + frp 提问服务

在 GitHub Actions（macOS runner）上：

1. 用 [CapSolver](https://www.capsolver.com/) 过 [claude.ai](https://claude.ai) 的 Cloudflare Turnstile
2. 用 [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm) 导入 `cookie.json` 登录你自己的账号
3. 常驻 **OpenAI 兼容 API**（`/v1/chat/completions`），并用 [frp](https://github.com/fatedier/frp) 暴露到公网；会话期内（默认 30 分钟）可多次提问，**流式 SSE** 实时返回

## 输入与连接

- **minutes**：服务存活时长（分钟），默认 `30`。期间可多次提问（同一对话，保留上下文）。
- **cookie.json**：仓库根目录的 Cookie-Editor JSON（需含 `.claude.ai` / `claude.ai` 的 cookie，尤其是 `sessionKey`）。
- **Base URL**：`http://<frps_ip>:<remotePort>/v1`（见 `frpc.toml`，默认 `http://8.210.199.147:6000/v1`）
- **模型名**：`claude`
- **API Key**：任意非空字符串即可；若设置了 `SERVE_API_KEY` 则需与之匹配

### 客户端配置示例

**OpenAI Python SDK**

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://8.210.199.147:6000/v1",
    api_key="sk-local",  # 任意；若设了 SERVE_API_KEY 则填相同值
)
stream = client.chat.completions.create(
    model="claude",
    messages=[{"role": "user", "content": "用一句话介绍你自己"}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
print()
```

**curl（流式 SSE）**

```bash
curl -N http://8.210.199.147:6000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-local" \
  -d '{"model":"claude","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

**ChatBox / Cherry Studio 等**：Base URL 填 `http://8.210.199.147:6000/v1`，模型填 `claude`，API Key 填任意值。

客户端「新对话」时若 messages 里只有一条 user 消息，服务会自动在 claude.ai 开新会话。

## 运行环境

GitHub Actions 使用 **macOS** runner + **patchright Chromium**（系统 Google Chrome 不支持加载未打包扩展）。

本地 macOS / Windows 加载 Cookie-Editor 时同样应使用 patchright Chromium；未加载扩展时仍可用系统 Chrome。

## 使用方式

### 1. GitHub Actions 手动触发

1. 打开仓库 **Actions** 页，选择 **Serve Claude (frp)** → **Run workflow**
2. 填 **minutes**（默认 30），运行
3. 等约 2-3 分钟登录完成后，把客户端 Base URL 设为 `http://<frps_ip>:6000/v1` 即可提问

也可以用脚本一键触发并打印连接命令：

```bash
python scripts/trigger_actions.py 30      # 触发并提示如何连接
python scripts/trigger_actions.py --logs  # 打印最近一次运行日志（排错）
```

frp 配置见仓库根目录 `frpc.toml`：`serverAddr` / `auth.token` 为你的 frps，
`[[proxies]]` 把 runner 本机 `8787` 暴露到 frps 的 `remotePort`（默认 `6000`，
需在 frps 的 `allowPorts` 范围内）。frpc 版本在工作流 `FRP_VERSION` 控制（默认与
你的 frps 主版本尽量一致，否则可能握手失败）。

### 2. 本地运行

服务端（登录 + 提问服务）：

```bash
pip install -r requirements.txt
patchright install chromium
python scripts/install_extension.py extensions/cookie-editor
export LOAD_COOKIE_EXTENSION=true
export COOKIE_EDITOR_DIR="$PWD/extensions/cookie-editor"
export CAPSOLVER_API_KEY="CAP-XXXXXXXX"
cd scripts
python serve.py ../cookie.json --minutes 30 --port 8787
```

再开一个终端跑 frpc（暴露 8787）：`frpc -c frpc.toml`，然后用 OpenAI 客户端连 `http://127.0.0.1:8787/v1`。

单次提问（不起服务、跑完即退）仍可用：

```bash
cd scripts && python login.py ../cookie.json --prompt "用一句话介绍你自己。"
```

## 工作流程

1. 启动 Chrome（默认 [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) 反检测内核），加载 Cookie-Editor 扩展
2. 打开 `https://claude.ai/`，遇到 Cloudflare Turnstile 时由 CapSolver 自动求解
3. 打开 Cookie-Editor 弹窗，粘贴 `cookie.json` 并导入
4. 重新加载 claude.ai，检查是否存在 `sessionKey` cookie
5. 下载 frpc，后台连上 frps 并暴露本机端口
6. 启动 OpenAI 兼容 API（`serve.py`）：`POST /v1/chat/completions` 把最后一条 user 消息
   填入 claude.ai 并**流式 SSE** 返回；仅一条 user 消息时自动开新对话；会话到 `minutes` 后退出

## Cloudflare 自动验证（CapSolver）

**过 Turnstile 只有一种方式：CapSolver。** 在仓库 **Settings → Secrets and variables → Actions** 中添加 `CAPSOLVER_API_KEY`。

```bash
export CAPSOLVER_API_KEY="CAP-XXXXXXXX"
```

可选环境变量：

- `CAPSOLVER_API_BASE`：CapSolver API 地址，默认 `https://api.capsolver.com`
- `CAPSOLVER_SITEKEY`：手动指定 Turnstile sitekey（自动检测失败时兜底）
- `COOKIE_INPUT_FILE`：cookie 文件路径，默认 `cookie.json`
- `COOKIE_EDITOR_DIR`：Cookie-Editor 扩展目录
- `LOAD_COOKIE_EXTENSION`：是否加载扩展，默认 `true`
- `SERVE_PORT`：`serve.py` 本机监听端口，默认 `8787`（需与 `frpc.toml` 的 `localPort` 一致）
- `SERVE_MINUTES`：服务存活分钟数，默认 `30`（等价于 `--minutes`）
- `SERVE_API_KEY`：可选；设置后客户端 `Authorization: Bearer` 须匹配
- `CLAUDE_PROMPT`：单次提问模式（`login.py --prompt`）要问的问题

## 注意事项

- Cookie 包含账号会话信息，请勿泄露
- Cloudflare Turnstile 可能导致偶发失败，macOS + 系统 Chrome 成功率更高，可重试
- 仅供个人账号管理使用
