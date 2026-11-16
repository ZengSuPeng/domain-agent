## 本地开发

```powershell
cd d:\workstation\运营\domain_agent
uv run uvicorn domain_agent.main:app --host 127.0.0.1 --port 8001 --reload
```

后端在 <http://127.0.0.1:8001> 同源托管 `frontend/dist`，浏览器直接打开即可。

后端启动时会自动读取仓库根目录的 `.env`（已 gitignore），按 `.env.example` 复制一份填即可；
Render 上直接在控制台配环境变量（已存在的变量优先，`.env` 不会覆盖）。

## 飞书登录授权

登录走飞书开放平台 OAuth 2.0 授权码模式（浏览器整页跳转，飞书授权页自带扫码/账密）。
后端只签发会话 token，不接触用户密码。

### 1. 开放平台配置

1. <https://open.feishu.cn/app> → 创建「企业自建应用」→ 添加「网页应用」能力。
2. 开发配置 → 安全设置 → 重定向 URL，添加（必须完全一致，含协议和端口）：
   - 本地：`http://127.0.0.1:8001/api/auth/feishu/callback`
   - 生产：`https://domain-agent-33im.onrender.com/api/auth/feishu/callback`
3. 开发配置 → 权限管理：登录只取姓名/头像/open_id 不需要额外权限；
   若用邮箱白名单（`AUTH_ALLOWED_EMAILS`）需申请 `contact:user.email:readonly`，
   并设置 `FEISHU_SCOPE=contact:user.email:readonly`（否则 user_info 不返回邮箱）。
4. 版本管理与发布 → 创建版本并发布，确认应用对成员可见。

### 2. 后端环境变量

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `FEISHU_APP_ID` | 是 | 应用凭证 App ID |
| `FEISHU_APP_SECRET` | 是 | 应用凭证 App Secret |
| `AUTH_SECRET` | 推荐 | session 签名密钥；不填则从 App Secret 派生 |
| `AUTH_SESSION_HOURS` | 否 | 登录有效期，默认 12 小时 |
| `FEISHU_REDIRECT_URI` | 否 | 不填自动按请求域名拼 `/api/auth/feishu/callback` |
| `FEISHU_SCOPE` | 否 | 授权时申请的用户权限，空格分隔（邮箱白名单需设 `contact:user.email:readonly`） |
| `AUTH_ALLOWED_EMAILS` | 否 | 邮箱白名单，支持精确邮箱和 `*@corp.com` |
| `AUTH_ALLOWED_OPEN_IDS` | 否 | open_id 白名单 |
| `AUTH_ALLOWED_TENANT_KEYS` | 否 | 租户白名单（`tenant_key`），配了即只允许本企业租户 |
| `AUTH_REQUIRED` | 生产建议 `1` | 强制启用登录：凭证漏配时拒绝请求（503），而不是退化为无登录 |
| `AUTH_DISABLED` | 否 | 设为 `1` 强制关闭登录（仅本地/内网调试，生产不要设） |

- 白名单全部留空 = 应用可见范围内的所有飞书成员都能登录。
- `AUTH_ALLOWED_TENANT_KEYS` 是强制门槛，可与其他白名单叠加使用。
- `CORS_ORIGINS` 必须包含前端真实 Origin（Cloudflare Pages 域名），登录回跳也按它校验。
- 前端独立部署时可用 `FRONTEND_URL` 指定默认回跳地址。
- 登录态是 HMAC 签名的无状态 token：退出登录只清浏览器本地 token。若要立即吊销
  所有人的会话，换一个 `AUTH_SECRET` 即可（用 App Secret 派生时换 App Secret 同效）。

未配置任何飞书凭证时，后端打印 `[auth] 未启用登录校验` 并放行请求，本地开发无需登录；
只配了其中一项属于「想启用但没配全」，此时所有 `/api/*` 返回 503，避免误以为已受保护。

### 3. 登录接口与前端行为

| 接口 | 作用 |
| --- | --- |
| `GET /api/auth/feishu/url` | 生成飞书授权页地址（state 已签名，含回跳地址） |
| `GET /api/auth/feishu/callback` | 飞书回调：code 换用户信息 → 签一次性 ticket → 跳回前端 |
| `POST /api/auth/feishu/exchange` | 用 ticket 换正式 session token |
| `GET /api/auth/me` | 查询当前登录态 |
| `POST /api/auth/logout` | 退出登录（无状态 token，前端丢弃即可） |

除上述接口外，所有 `/api/*` 都要求 `Authorization: Bearer <token>`。前端把 token 存
`localStorage`，axios 与 SSE 请求自动携带；收到 401 会清掉登录态并回到飞书登录页。
顶栏右上角显示飞书头像与姓名，菜单里可退出登录。

本地想跳过登录：不配 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`，或显式设 `AUTH_DISABLED=1`。

## 前端构建（两种模式，别选错）

`frontend/.env.production` 里写的是**生产后端**地址（Render），`vite build` 默认
就是 production 模式，会把绝对地址打进 bundle。本地必须用 localdev 模式，
否则页面会把请求发到生产环境（在本地点「生成」还会写进生产库）。

```powershell
cd frontend
$env:PATH = "..\node;" + $env:PATH   # 本机 node 在仓库的 node\ 目录

npm run build:local   # 本地联调：baseURL = /api，请求打到当前页面所在主机
npm run build         # 部署 Cloudflare Pages：baseURL = Render 绝对地址
```

默认 `frontend/dist` 已被 `.gitignore` 排除，Render 上也不托管前端（只能返回
占位页），所以生产构建产物要上传到 Cloudflare Pages。

## 部署上线（Render + Cloudflare Pages）

### 0. 上线前准备

- 本地 `.env` 不要提交（已在 `.gitignore`），Render 上单独配环境变量
- 飞书开放平台「安全设置 → 重定向 URL」加入生产回调地址
- 飞书应用可用范围保持「部分成员」，对外共享保持关闭
- 生成一个固定的会话密钥：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### 1. 后端（Render）

| 配置项 | 值 |
| --- | --- |
| Runtime | Python 3.12 |
| Root Directory | 仓库根目录 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn --app-dir src domain_agent.main:app --host 0.0.0.0 --port $PORT` |
| Health Check Path | `/` |

> 如果现有服务已经能正常启动，Start Command 沿用即可；关键只有一点：
> 仓库是 `src/` 布局，`domain_agent` 必须在 import path 上
> （`--app-dir src` 或 `PYTHONPATH=src`）。

环境变量：

| 变量 | 值 |
| --- | --- |
| `FEISHU_APP_ID` | 飞书应用 App ID |
| `FEISHU_APP_SECRET` | 飞书应用 App Secret |
| `FEISHU_REDIRECT_URI` | `https://domain-agent-33im.onrender.com/api/auth/feishu/callback` |
| `FRONTEND_URL` | Cloudflare Pages 域名，如 `https://domain-agent.pages.dev` |
| `CORS_ORIGINS` | 同上（多个用逗号分隔，不要带尾部斜杠） |
| `AUTH_REQUIRED` | `1` |
| `AUTH_SECRET` | 上一步生成的随机串 |
| `AUTH_SESSION_HOURS` | `12`（可选） |
| `DEEPSEEK_API_KEY` 等 | 沿用现有配置 |

- **不要**设置 `AUTH_DISABLED`。
- `AUTH_REQUIRED=1` 的作用：万一 `FEISHU_APP_ID/SECRET` 漏配，服务会对所有
  `/api/*` 返回 503，而不是静默变成「谁都能访问」。
- 可选：`AUTH_ALLOWED_TENANT_KEYS` 填本企业 `tenant_key`，即使以后应用可见范围
  被改大或开了关联组织共享，非本企业租户也会被后端拒绝。

### 2. 前端（Cloudflare Pages）

| 配置项 | 值 |
| --- | --- |
| Root Directory | `frontend` |
| Build Command | `npm run build` |
| Build Output Directory | `dist` |
| 环境变量 | 不需要（`.env.production` 已内置 Render 地址）；建议设 `NODE_VERSION=20` |

首次部署拿到 `https://xxx.pages.dev` 后，把它填回 Render 的
`CORS_ORIGINS` 和 `FRONTEND_URL`，再重新部署一次后端。

### 3. 飞书开放平台

在「开发配置 → 安全设置 → 重定向 URL」中同时保留：

- 本地：`http://127.0.0.1:8001/api/auth/feishu/callback`
- 生产：`https://domain-agent-33im.onrender.com/api/auth/feishu/callback`

然后发布应用版本，确认可用范围仍是「部分成员」。

### 4. 上线验证

1. 打开 Cloudflare Pages 域名，应看到飞书登录页（Render 冷启动时首屏最多等 60s 左右）
2. 扫码登录，应回到 Pages 域名，顶栏显示你的飞书姓名
3. 未登录直接访问 `https://<render>/api/unavailable-domains` 应返回 401
4. Render 日志应出现：
   - `[auth] 飞书登录已启用（configured=True, ...）`
   - `[cors] 允许的前端 Origin：https://xxx.pages.dev`
5. 用一个不在可用范围的账号扫码，应被飞书拒绝（20010），不会跳回 Pages

### 5. 本地与生产构建别混用

- 生产 / Pages：`npm run build`（API 指向 Render 绝对地址）
- 本地联调：`npm run build:local`（API 指向当前页面所在主机）

在本地跑 `npm run build` 后，页面的请求会打到 Render 生产环境，别在本地点
「生成」或「入库」。
