# Claude Cookie 登录并提问

通过 GitHub Actions 或本地脚本：

1. 用 [CapSolver](https://www.capsolver.com/) 过 [claude.ai](https://claude.ai) 的 Cloudflare Turnstile
2. 用 [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm) 导入 `cookie.json` 登录你自己的账号
3. 在 claude.ai 新建对话，提出问题并抓取回答（写入运行摘要、`answer.md` 与日志）

## 输入

- **问题（prompt）**：通过 GitHub Actions 的 `Run workflow` 表单（或本地 `--prompt`）输入，用你的账号在 claude.ai 提问。
- **cookie.json**：仓库根目录的 Cookie-Editor JSON（需包含 `.claude.ai` / `claude.ai` 域名的 cookie，尤其是 `sessionKey`）。

## 运行环境

GitHub Actions 使用 **macOS** runner + **patchright Chromium**（系统 Google Chrome 不支持加载未打包扩展）。

本地 macOS / Windows 加载 Cookie-Editor 时同样应使用 patchright Chromium；未加载扩展时仍可用系统 Chrome。

## 使用方式

### 1. GitHub Actions 手动触发

1. 打开仓库 **Actions** 页
2. 选择 **Ask Claude**
3. 点击 **Run workflow**
4. 在 **问题** 输入框填入要问 claude.ai 的内容（`cookie.json` 默认即可）
5. 运行完成后：
   - 在 **Summary** 顶部直接查看「问题 / 回答」
   - 或在 Artifacts 中下载 `claude-answer`（`answer.md` / `answer.txt`）与 `claude-after-import` 截图

也可以用脚本一键触发并在本地打印回答：

```bash
python scripts/trigger_actions.py "用一句话介绍你自己。"
```

### 2. 本地运行（macOS / Windows 推荐）

```bash
pip install -r requirements.txt
patchright install chromium
python scripts/install_extension.py extensions/cookie-editor
export LOAD_COOKIE_EXTENSION=true
export COOKIE_EDITOR_DIR="$PWD/extensions/cookie-editor"
export CAPSOLVER_API_KEY="CAP-XXXXXXXX"
cd scripts
python login.py ../cookie.json --prompt "用一句话介绍你自己。"
```

- 回答保存在 `scripts/debug/answer.md` 与 `scripts/debug/answer.txt`，并打印在日志中。
- 登录结果截图保存在 `scripts/debug/claude-after-import.png`，提问后截图为 `scripts/debug/claude-answer.png`。
- 不带 `--prompt` 时只登录并截图，不提问。

## 工作流程

1. 启动 Chrome（默认 [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) 反检测内核），加载 Cookie-Editor 扩展
2. 打开 `https://claude.ai/`，遇到 Cloudflare Turnstile 时由 CapSolver 自动求解
3. 打开 Cookie-Editor 弹窗，粘贴 `cookie.json` 并导入
4. 重新加载 claude.ai，检查是否存在 `sessionKey` cookie
5. 保存全页截图到 `debug/claude-after-import.png`
6. 若提供了问题：在 claude.ai 新建对话提问，等待回答输出结束后抓取（DOM + 内部 API 兜底），写入 `debug/answer.md`、日志与运行摘要

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
- `CLAUDE_PROMPT`：要向 claude.ai 提的问题（等价于 `--prompt`）

## 注意事项

- Cookie 包含账号会话信息，请勿泄露
- Cloudflare Turnstile 可能导致偶发失败，macOS + 系统 Chrome 成功率更高，可重试
- 仅供个人账号管理使用
