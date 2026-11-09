import json
import os
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from domain_agent.deepseek import generate_domain, generate_domain_stream, get_balance
from domain_agent.cloudfare import check_domain_availability
from domain_agent import gpt
from domain_agent.archive import check_domains_history
from domain_agent import unavailable as unavailable_db
from domain_agent.sources import trustpilot

app = FastAPI()


@app.on_event("startup")
async def startup():
    """启动时初始化不可用域名库（PostgreSQL 建表；无 DB 时回退到 JSON）。"""
    unavailable_db.init_db()
    # 后台预热 Trustpilot 浏览器：先让 AWS WAF 挑战走完、把 token 写进上下文，
    # 否则服务刚启动后的第一批域名会整批卡在挑战页上。
    # 放后台线程，不阻塞启动（首次 Chromium 冷启动要几秒）。
    threading.Thread(
        target=lambda: print(f"[Trustpilot] {trustpilot.warmup()}"),
        daemon=True,
        name="trustpilot-warmup",
    ).start()

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


# ============ API ============
@app.post("/api/generate")
async def api_generate(payload: dict):
    theme = payload.get("theme", "")
    desc = payload.get("desc", "")
    exclude = payload.get("exclude", []) or []
    result = generate_domain(theme, desc, exclude=exclude)
    return {
        "success": True,
        "data": result.get("domains", []),
        "thinking": result.get("thinking", ""),
    }


def _sse_stream(theme, desc, exclude=None):
    """将生成器事件包装为 SSE 文本流（不过滤域名，由前端调用 /api/check-domains 检测）。"""
    for event in generate_domain_stream(theme, desc, exclude=exclude):
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
    exclude = payload.get("exclude", []) or []
    return StreamingResponse(
        _sse_stream(theme, desc, exclude=exclude),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/deepseek-balance")
def api_deepseek_balance():
    """查询 DeepSeek 账户余额（GET https://api.deepseek.com/user/balance）。

    用同步 def：内部是阻塞式 requests，交给线程池，别卡事件循环。
    失败时 success=False，message 里带原因（未配 Key / Key 无效 / 超时）。
    """
    try:
        return {"success": True, "data": get_balance()}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/api/dmxapi-balance")
def api_dmxapi_balance():
    """查询 DMXAPI（模型中转平台）账户余额。

    合规检测用的 GPT 模型走的这个平台，但余额接口需要「系统访问令牌 + 用户ID」，
    跟调模型的 sk-... key 不是一回事（见 gpt.get_balance_credentials）。
    """
    try:
        return {"success": True, "data": gpt.get_balance()}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/api/check-domains")
def api_check_domains(payload: dict):
    """域名可注册性检测（Cloudflare Registrar API）。

    用同步 def 而非 async def：内部调用阻塞式 requests，放在线程池里执行，
    避免阻塞事件循环导致其他请求（含分源并行的历史检测）被一起卡死。
    """
    domains = payload.get("domains", [])
    if not domains:
        return {"success": False, "message": "domains 不能为空"}
    try:
        results = check_domain_availability(domains)
        return {"success": True, "data": results}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/api/check-compliance")
def api_check_compliance(payload: dict):
    """域名合规性及侵权风险检测（调用 gpt.py）。

    每个域名独立 try/except，部分失败不影响其他域名。
    入参 domains: [{"domain": "xvoma.com", "reason": "命名理由"}, ...]
    """
    domains = payload.get("domains", [])
    if not domains:
        return {"success": False, "message": "domains 不能为空"}

    results = []
    for item in domains:
        domain_name = item.get("domain", "") if isinstance(item, dict) else str(item)
        try:
            risk = gpt.main(None, item)
            results.append({
                "domain": risk.get("domain", domain_name),
                "risk_level": risk.get("risk_level", "未知"),
                "target_brand": risk.get("target_brand", ""),
                "risk_reason": risk.get("risk_reason", ""),
                "negative_review": risk.get("negative_review", ""),
                "reason": risk.get("reason", item.get("reason", "") if isinstance(item, dict) else ""),
            })
        except Exception as e:
            print(f"[Compliance] 域名 {domain_name} 检测异常: {e}")
            results.append({
                "domain": domain_name,
                "risk_level": "未知",
                "target_brand": "无",
                "risk_reason": f"检测失败 - {str(e)[:80]}",
                "negative_review": "暂无",
                "reason": item.get("reason", "") if isinstance(item, dict) else "",
            })

    return {"success": True, "data": results}


@app.post("/api/check-history")
def api_check_history(payload: dict):
    """域名历史使用痕迹检测（Wayback / Trustpilot / Gridinsoft / VirusTotal）。

    入参：
      domains: ["xvoma.com", "abc.com", ...]
      sources: 可选，只检测指定源，如 ["wayback"]。不传则检测全部源
               （VirusTotal 仅在其余源无命中时才调用，省额度）。
               前端按源分别发请求：慢源只阻塞自己，不再拖垮整批。

    返回 success 语义：
      - 全部成功 → success=True
      - 部分失败 → success=True, partial=True, failed>0（data 中对应项 status="partial"）
      - 全部失败 → success=False, data 仍返回（含逐条 error 原因）
    """
    domains = payload.get("domains", [])
    if not domains:
        return {"success": False, "message": "domains 不能为空"}
    sources = payload.get("sources")
    try:
        results = check_domains_history(domains, sources=sources)
        failed = [r for r in results if r.get("status") == "error"]
        partial = [r for r in results if r.get("status") == "partial"]
        total = len(results)

        if total > 0 and len(failed) == total:
            return {
                "success": False,
                "message": f"全部 {total} 个域名检测失败（网络不可达或境外服务超时）",
                "data": results,
            }

        notes = []
        if failed:
            notes.append(f"{len(failed)}/{total} 个域名全部源检测失败")
        if partial:
            notes.append(f"{len(partial)}/{total} 个域名部分源检测失败")

        return {
            "success": True,
            "partial": bool(failed or partial),
            "failed": len(failed),
            "partial_count": len(partial),
            "total": total,
            "message": "；".join(notes),
            "data": results,
        }
    except Exception as e:
        return {"success": False, "message": str(e)}


# ============ 历史痕迹检测·分源独立端点 ============
# 每个源一个独立 URL 路径：前端 Network 面板可直接看到 4 个请求，
# 且慢源只阻塞自己，不再拖垮整批。
def _check_history_single(payload: dict, source: str):
    domains = payload.get("domains", [])
    if not domains:
        return {"success": False, "message": "domains 不能为空"}
    try:
        results = check_domains_history(domains, sources=[source])
        failed = [r for r in results if r.get("status") == "error"]
        partial = [r for r in results if r.get("status") == "partial"]
        total = len(results)
        if total > 0 and len(failed) == total:
            return {
                "success": False,
                "message": f"全部 {total} 个域名 {source} 源检测失败",
                "data": results,
            }
        return {
            "success": True,
            "partial": bool(failed or partial),
            "failed": len(failed),
            "partial_count": len(partial),
            "total": total,
            "data": results,
        }
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/api/check-wayback")
def api_check_wayback(payload: dict):
    """Wayback Machine 历史存档检测（独立端点）。"""
    return _check_history_single(payload, "wayback")


@app.post("/api/check-trustpilot")
def api_check_trustpilot(payload: dict):
    """Trustpilot 评价检测（独立端点）。"""
    return _check_history_single(payload, "trustpilot")


@app.post("/api/check-gridinsoft")
def api_check_gridinsoft(payload: dict):
    """Gridinsoft 信誉报告检测（独立端点）。"""
    return _check_history_single(payload, "gridinsoft")


@app.post("/api/check-virustotal")
def api_check_virustotal(payload: dict):
    """VirusTotal 检测（独立端点；前端仅在其余源无命中时调用以省额度）。"""
    return _check_history_single(payload, "virustotal")


# ============ 不可用域名库 ============
@app.get("/api/unavailable-domains")
async def api_get_unavailable():
    """获取不可用域名库列表。"""
    return unavailable_db.load_domains()


@app.post("/api/unavailable-domains")
async def api_add_unavailable(payload: dict):
    """批量添加不可用域名。入参 items: [{"domain": "...", "reason": "..."}]"""
    items = payload.get("items", [])
    if not items:
        return {"success": False, "message": "items 不能为空"}
    added = unavailable_db.add_domains(items)
    return {"success": True, "added": added, "total": len(unavailable_db.load_domains())}


@app.delete("/api/unavailable-domains/{domain}")
async def api_remove_unavailable(domain: str):
    """从不可用域名库移除单个域名。"""
    ok = unavailable_db.remove_domain(domain)
    return {"success": ok}


# ============ SPA 回退（必须放在所有 API 路由之后） ============
@app.get("/{full_path:path}", response_class=HTMLResponse)
async def spa_fallback(request: Request, full_path: str):
    """SPA 路由回退：非 /api 路径统一返回 index.html。"""
    if full_path.startswith("api"):
        return HTMLResponse("Not Found", status_code=404)
    index_file = FRONTEND_DIST / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse("Not Found", status_code=404)
