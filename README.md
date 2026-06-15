# CursorCookie

通过 GitHub Actions 自动使用邮箱验证码登录 [cursor.com](https://cursor.com)，并以 [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm) JSON 格式导出 cookie。

## 输入格式

```
邮箱----邮箱密码
```

示例：

```
SapphiraCaelum5932@outlook.com----rq757721
```

验证码从 [星辰邮箱大师](https://www.xckj.site/easy-mailbox/) 获取。

## 运行环境

GitHub Actions 使用 **macOS** runner + **系统 Google Chrome**。

根据 [GitHub Actions macOS 镜像文档](https://github.com/actions/runner-images/blob/main/images/macos/macos-15-Readme.md)，`macos-latest` 已预装 Google Chrome，**无需额外安装**。相比 Ubuntu 无头环境，macOS 真实 Chrome 更容易通过 Cursor 登录页的 Cloudflare 验证。

本地 macOS 可直接使用系统 Chrome；Windows 同样支持；Linux 回退 Playwright Chromium。

## 使用方式

### 1. GitHub Actions 手动触发

1. 打开仓库 **Actions** 页
2. 选择 **Get Cursor Cookie**
3. 点击 **Run workflow**
4. 在 `account` 输入框填入凭证，例如：
   ```
   SapphiraCaelum5932@outlook.com----rq757721
   ```
5. 运行完成后在日志和 Artifacts 中查看 `cursor-cookies.json`

### 2. 本地运行（macOS / Windows 推荐）

```bash
pip install -r requirements.txt
export PLAYWRIGHT_CHANNEL=chrome   # Windows: set PLAYWRIGHT_CHANNEL=chrome
cd scripts
python login.py "SapphiraCaelum5932@outlook.com----rq757721"
```

macOS / Windows 若已安装系统 Chrome，无需执行 `playwright install chrome`（GitHub Actions 直接使用预装 Chrome）。

Linux 回退方案：

```bash
pip install -r requirements.txt
playwright install chromium
cd scripts
python login.py "邮箱----密码"
```

## 工作流程

1. 解析 `邮箱----密码` 凭证
2. 加载 [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm) 浏览器扩展
3. 打开 `https://authenticator.cursor.sh/` 输入邮箱
4. 从 `https://www.xckj.site/easy-mailbox/frontend?email=...&password=...` 对应 API 轮询验证码
5. 填入 6 位验证码完成登录
6. 访问 `cursor.com` 并导出 cookie（Cookie-Editor JSON 格式）

## 输出示例

```json
[
  {
    "domain": ".cursor.com",
    "hostOnly": false,
    "httpOnly": true,
    "name": "WorkosCursorSessionToken",
    "path": "/",
    "sameSite": "lax",
    "secure": true,
    "session": false,
    "storeId": "0",
    "value": "..."
  }
]
```

## Cloudflare 自动验证（CapSolver）

Cursor 登录页有 Cloudflare Turnstile。**Turnstile 一律由 [CapSolver](https://www.capsolver.com/) 求解**（原先的 cliclick 真人点击方案实测无效，已移除）：

1. 用 Playwright 提取页面 Turnstile 的 `sitekey`
2. 调 CapSolver `AntiTurnstileTaskProxyLess` 任务拿到 token
3. 把 token 写回页面的 `cf-turnstile-response` 字段并触发 `turnstile.render` 回调

启用方式：设置环境变量 `CAPSOLVER_API_KEY`（GitHub Actions 在仓库 **Settings → Secrets and variables → Actions** 中添加同名 secret）。**未配置则无法通过 Turnstile。**

```bash
export CAPSOLVER_API_KEY="CAP-XXXXXXXX"
```

可选环境变量：`CAPSOLVER_API_BASE`（默认 `https://api.capsolver.com`）。

> 说明：CapSolver 走 proxyless 求解，token 由其服务器 IP 生成。多数内嵌 Turnstile widget 可直接通过；
> 若 Cloudflare 对该站点做了强 IP 绑定，可改用 CapSolver 的 `AntiTurnstileTask`（带 proxy）。

> 注：`cliclick` 仍会安装，但仅用于 `human_mouse` 的真人光标输入（填邮箱/点按钮），与过 Turnstile 无关。

## 注意事项

- Cookie 包含账号会话信息，请勿泄露
- Cloudflare Turnstile 可能导致偶发失败，macOS + 系统 Chrome 成功率更高，可重试
- 仅供个人账号管理使用
