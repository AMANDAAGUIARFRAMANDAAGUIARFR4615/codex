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
2. 启动 Chrome（默认用 [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) 反检测内核），并注入 CapSolver 的 `turnstile.render` hook
3. 打开 `https://authenticator.cursor.sh/` 输入邮箱
4. 遇到 Cloudflare Turnstile 时由 [CapSolver](https://www.capsolver.com/) 自动求解（详见下文）
5. 从 `https://www.xckj.site/easy-mailbox/frontend?email=...&password=...` 对应 API 轮询验证码
6. 填入 6 位验证码完成登录
7. 访问 `cursor.com` 并导出 cookie（Cookie-Editor JSON 格式）

> Cookie-Editor 扩展为可选项，默认关闭（`LOAD_COOKIE_EXTENSION=false`）；cookie 直接通过 Playwright 读取并导出，无需扩展。

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

Cursor 登录页有 Cloudflare Turnstile。**过 Turnstile 只有一种方式：[CapSolver](https://www.capsolver.com/)。** 原先基于真人鼠标轨迹 / `cliclick` 的「过验证」方案实测无效，已全部移除（连同 `human_mouse.py`、`turnstile_solver.py`）。

工作原理（见 `scripts/capsolver_solver.py`）：

1. 页面脚本执行前注入 hook，劫持 `window.turnstile.render`，捕获每个 widget 的 `sitekey / action / cData` 与 `callback`。Cursor 用的是**隐形 Turnstile**（无可见复选框、也无 `data-sitekey` 节点），sitekey 只能从这里拿到
2. 检测到 Turnstile 后调 CapSolver `AntiTurnstileTaskProxyLess` 任务，轮询拿到 token
3. 把 token 写回页面的 `cf-turnstile-response` 字段并触发被 hook 捕获的回调，让页面继续登录

启用方式：设置环境变量 `CAPSOLVER_API_KEY`（GitHub Actions 在仓库 **Settings → Secrets and variables → Actions** 中添加同名 secret）。**未配置则无法通过 Turnstile。**

```bash
export CAPSOLVER_API_KEY="CAP-XXXXXXXX"
```

脚本启动时会调用 CapSolver `getBalance` 打印 Key 是否有效、账户余额，方便确认对接是否正常。整个求解过程都会打印 `[capsolver]` 前缀日志（检测 sitekey → 提交任务 → 轮询 → 注入 token）。

可选环境变量：

- `CAPSOLVER_API_BASE`：CapSolver API 地址，默认 `https://api.capsolver.com`
- `CAPSOLVER_SITEKEY`：手动指定 Turnstile sitekey（仅当上述自动检测都失败时作兜底）

> 说明：CapSolver 走 proxyless 求解，token 由其服务器 IP 生成。多数内嵌 Turnstile widget 可直接通过；
> 若 Cloudflare 对该站点做了强 IP 绑定，可改用 CapSolver 的 `AntiTurnstileTask`（带 proxy）。

## 注意事项

- Cookie 包含账号会话信息，请勿泄露
- Cloudflare Turnstile 可能导致偶发失败，macOS + 系统 Chrome 成功率更高，可重试
- 仅供个人账号管理使用
