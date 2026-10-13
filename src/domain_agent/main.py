import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from domain_agent.domain import generate_domain, generate_domain_stream
from domain_agent.cloudfare import check_domain_availability
from domain_agent import gpt

app = FastAPI()

# CORS：允许前端（Vercel 或本地 5173）跨域调用
ALLOWED_ORIGINS = os.environ.get("CORS_ORIGINS", "").split(",") if os.environ.get("CORS_ORIGINS") else [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"


# ============ 静态资源（生产环境：Vue 构建产物） ============
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """根路径：优先返回 Vue 构建后的 index.html，不存在则返回占位页。"""
    index_file = FRONTEND_DIST / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse(
        "<h1>Backend is running</h1>"
        "<p>前端未构建，请在 <code>frontend/</code> 目录下执行 <code>npm run build</code>。</p>"
    )


@app.get("/{full_path:path}", response_class=HTMLResponse)
async def spa_fallback(request: Request, full_path: str):
    """SPA 路由回退：非 /api 路径统一返回 index.html。"""
    if full_path.startswith("api"):
        return HTMLResponse("Not Found", status_code=404)
    index_file = FRONTEND_DIST / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse("Not Found", status_code=404)


# ============ API ============
@app.post("/api/generate")
async def api_generate(payload: dict):
    theme = payload.get("theme", "")
    desc = payload.get("desc", "")
    result = generate_domain(theme, desc)
    return {
        "success": True,
        "data": result.get("domains", []),
        "thinking": result.get("thinking", ""),
    }


def _sse_stream(theme, desc):
    """将生成器事件包装为 SSE 文本流（不过滤域名，由前端调用 /api/check-domains 检测）。"""
    for event in generate_domain_stream(theme, desc):
        etype = event["type"]
        if etype == "thinking":
            yield f"event: thinking\ndata: {json.dumps(event['content'], ensure_ascii=False)}\n\n"
        elif etype == "done":
            yield f"event: done\ndata: {json.dumps(event['domains'], ensure_ascii=False)}\n\n"
        elif etype == "error":
            yield f"event: error\ndata: {json.dumps(event['message'], ensure_ascii=False)}\n\n"


@app.post("/api/generate/stream")
async def api_generate_stream(payload: dict):
    theme = payload.get("theme", "")
    desc = payload.get("desc", "")
    return StreamingResponse(
        _sse_stream(theme, desc),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/check-domains")
async def api_check_domains(payload: dict):
    """域名可注册性检测（Cloudflare Registrar API）。"""
    domains = payload.get("domains", [])
    if not domains:
        return {"success": False, "message": "domains 不能为空"}
    try:
        results = check_domain_availability(domains)
        return {"success": True, "data": results}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/api/check-compliance")
async def api_check_compliance(payload: dict):
    """域名合规性及侵权风险检测（调用 gpt.py）。

    入参 domains: [{"domain": "xvoma.com", "reason": "命名理由"}, ...]
    """
    domains = payload.get("domains", [])
    if not domains:
        return {"success": False, "message": "domains 不能为空"}
    try:
        results = []
        for item in domains:
            risk = gpt.main(None, item)
            results.append({
                "domain": risk.get("domain", item.get("domain", "")),
                "risk_level": risk.get("risk_level", "未知"),
                "target_brand": risk.get("target_brand", ""),
                "risk_reason": risk.get("risk_reason", ""),
                "negative_review": risk.get("negative_review", ""),
                "reason": risk.get("reason", item.get("reason", "")),
            })
        return {"success": True, "data": results}
    except Exception as e:
        return {"success": False, "message": str(e)}
