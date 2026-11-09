## 本地开发

```powershell
cd d:\workstation\运营\domain_agent
uv run uvicorn domain_agent.main:app --host 127.0.0.1 --port 8001 --reload
```

后端在 <http://127.0.0.1:8001> 同源托管 `frontend/dist`，浏览器直接打开即可。

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
