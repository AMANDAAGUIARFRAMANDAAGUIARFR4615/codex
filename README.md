# Claude Cookie Import

通过 GitHub Actions 或本地脚本：

1. 用 [CapSolver](https://www.capsolver.com/) 过 [claude.ai](https://claude.ai) 的 Cloudflare Turnstile
2. 用 [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm) 导入 `cookie.json`
3. 重新加载页面并截图，供核验是否登录成功

## 输入

仓库根目录的 `cookie.json`（Cookie-Editor JSON 格式，需包含 `.claude.ai` / `claude.ai` 域名的 cookie，尤其是 `sessionKey`）。

## 运行环境

GitHub Actions 使用 **macOS** runner + **patchright Chromium**（系统 Google Chrome 不支持加载未打包扩展）。

本地 macOS / Windows 加载 Cookie-Editor 时同样应使用 patchright Chromium；未加载扩展时仍可用系统 Chrome。

## 使用方式

### 1. GitHub Actions 手动触发

1. 打开仓库 **Actions** 页
2. 选择 **Import Claude Cookie**
3. 点击 **Run workflow**
4. 默认使用 `cookie.json`，也可指定其他路径
5. 运行完成后在 Artifacts 中下载 `claude-after-import` 截图

### 2. 本地运行（macOS / Windows 推荐）

```bash
pip install -r requirements.txt
patchright install chromium
python scripts/install_extension.py extensions/cookie-editor
export LOAD_COOKIE_EXTENSION=true
export COOKIE_EDITOR_DIR="$PWD/extensions/cookie-editor"
export CAPSOLVER_API_KEY="CAP-XXXXXXXX"
cd scripts
python login.py ../cookie.json
```

截图保存在 `scripts/debug/claude-after-import.png`。

## 工作流程

1. 启动 Chrome（默认 [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) 反检测内核），加载 Cookie-Editor 扩展
2. 打开 `https://claude.ai/`，遇到 Cloudflare Turnstile 时由 CapSolver 自动求解
3. 打开 Cookie-Editor 弹窗，粘贴 `cookie.json` 并导入
4. 重新加载 claude.ai，检查是否存在 `sessionKey` cookie
5. 保存全页截图到 `debug/claude-after-import.png`

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

## 注意事项

- Cookie 包含账号会话信息，请勿泄露
- Cloudflare Turnstile 可能导致偶发失败，macOS + 系统 Chrome 成功率更高，可重试
- 仅供个人账号管理使用
