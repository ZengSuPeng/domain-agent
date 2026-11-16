import json
import os
import re
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from domain_agent import auth
from domain_agent.deepseek import generate_domain, generate_domain_stream, get_balance
from domain_agent.cloudfare import check_domain_availability
from domain_agent import gpt
from domain_agent.archive import check_domains_history
from domain_agent import unavailable as unavailable_db
from domain_agent.sources import trustpilot

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# .env 行格式：KEY=VALUE，也兼容 KEY: VALUE 和 export KEY=VALUE
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(.*?)\s*$")


def _load_dotenv():
    """读取项目根目录的 .env（本地开发用；Render 上直接配环境变量即可）。

    只补缺失的键，不覆盖已存在的环境变量，所以生产环境变量永远优先。
    兼容 `KEY=VALUE` / `KEY: VALUE`（冒号便于直接照抄飞书文档）/
    `export KEY=VALUE` 三种写法，并用 utf-8-sig 兼容记事本保存的 BOM。
    """
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_dotenv()

app = FastAPI()


@app.on_event("startup")
async def startup():
    """启动时初始化不可用域名库（PostgreSQL 建表；无 DB 时回退到 JSON）。"""
    unavailable_db.init_db()
    st = auth.status()
    if st["enabled"]:
        print(
            "[auth] 飞书登录已启用"
            f"（configured={st['configured']}, allowlist={st['allowlist_configured']}）"
        )
    else:
        print(f"[auth] 未启用登录校验：{st['reason'] or '未配置 FEISHU_APP_ID / FEISHU_APP_SECRET'}")
    print(f"[cors] 允许的前端 Origin：{', '.join(ALLOWED_ORIGINS)}")
    # 后台预热 Trustpilot 浏览器：先让 AWS WAF 挑战走完、把 token 写进上下文，
    # 否则服务刚启动后的第一批域名会整批卡在挑战页上。
    # 放后台线程，不阻塞启动（首次 Chromium 冷启动要几秒）。
    threading.Thread(
        target=lambda: print(f"[Trustpilot] {trustpilot.warmup()}"),
        daemon=True,
        name="trustpilot-warmup",
    ).start()

# CORS：允许前端（Cloudflare Pages 或本地 5173）跨域调用
# 统一做去空格/去尾部斜杠，避免 Render 面板里手写 "https://x.pages.dev, https://y" 时匹配失败
_raw_origins = (os.environ.get("CORS_ORIGINS") or "").strip()
ALLOWED_ORIGINS = (
    [item.strip().rstrip("/") for item in _raw_origins.split(",") if item.strip()]
    if _raw_origins
    else ["http://localhost:5173", "http://127.0.0.1:5173"]
)


@app.middleware("http")
async def feishu_auth_guard(request: Request, call_next):
    """登录校验：未登录一律拒绝 /api/* 请求（登录流程端点除外）。

    中间件顺序有讲究：Starlette 的 add_middleware 是「后加的更靠外层」，
    所以本中间件必须先于 CORSMiddleware 注册（CORS 在文件下方 add），
    这样 CORS 包在最外层——401/503 响应同样带 CORS 头，跨域前端才能
    读到状态码并触发「登录过期」逻辑，而不是被浏览器当成网络错误。
    """
    path = request.url.path
    if request.method == "OPTIONS" or not path.startswith("/api/"):
        return await call_next(request)
    # 登录流程本身不需要登录态（容忍末尾多余斜杠）
    if (path.rstrip("/") or "/") in auth.PUBLIC_API_PATHS:
        return await call_next(request)

    st = auth.status()
    if not st["enabled"]:
        return await call_next(request)
    if not st["configured"]:
        return JSONResponse(
            {"success": False, "code": "auth_misconfigured", "message": st["reason"]},
            status_code=503,
        )
    user = auth.user_from_request(request)
    if not user:
        return JSONResponse(
            {"success": False, "code": "unauthorized", "message": "未登录或登录已过期"},
            status_code=401,
        )
    request.state.user = user
    return await call_next(request)


# 注意：必须在上面注册完鉴权中间件后再 add_middleware(CORS)，否则 CORS 会被包在内层
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


# ============ 飞书登录 ============
# 说明：真实登录走浏览器整页跳转（飞书授权页自带扫码/账密登录），
# 后端只负责签发/校验登录态，不接触用户密码。
@app.get("/api/auth/me")
def api_auth_me(request: Request):
    """查询当前登录态；未启用登录时返回一个本地用户，前端直接进主界面。"""
    st = auth.status()
    if not st["enabled"]:
        return {
            "success": True,
            "authenticated": True,
            "auth_enabled": False,
            "feishu_configured": False,
            "user": {"name": "本地开发", "email": "", "avatar": "", "open_id": ""},
            "message": st["reason"],
        }

    user = auth.user_from_request(request)
    if not user:
        return {
            "success": True,
            "authenticated": False,
            "auth_enabled": True,
            "feishu_configured": st["configured"],
            "allowlist_configured": st["allowlist_configured"],
            "message": st["reason"],
        }

    return {
        "success": True,
        "authenticated": True,
        "auth_enabled": True,
        "feishu_configured": True,
        "user": auth.public_user(user),
        "expires_at": user.get("exp"),
    }


@app.get("/api/auth/feishu/url")
def api_feishu_login_url(request: Request, redirect: str = ""):
    """生成飞书授权页地址（前端拿到后整页跳转过去）。"""
    st = auth.status()
    if not st["enabled"]:
        return {"success": False, "message": "飞书登录未启用"}
    if not st["configured"]:
        return {"success": False, "message": st["reason"] or "飞书登录未配置"}

    # 只接受白名单 origin，防开放重定向；非法/缺省时回退到默认前端地址
    target = auth.safe_redirect(request, redirect) or auth.default_frontend(request)
    redirect_url = auth.redirect_uri(request)
    state = auth.new_state(target)
    return {
        "success": True,
        "data": {
            "url": auth.build_authorize_url(state, redirect_url),
            "redirect_uri": redirect_url,
        },
    }


@app.get("/api/auth/feishu/callback")
def api_feishu_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """飞书授权回调：code 换用户信息 -> 签一次性 ticket -> 跳回前端。"""
    # state 已签名，能还原出登录成功后的前端回跳地址；再校验一次 origin 防篡改
    target = (
        auth.safe_redirect(request, auth.validate_state(state) or "")
        or auth.default_frontend(request)
    )

    if error:
        message = (
            "已取消飞书授权"
            if error in ("access_denied", "user_cancelled")
            else f"飞书授权失败：{error}"
        )
        return RedirectResponse(auth.append_query(target, login_error=message))

    st = auth.status()
    if not st["enabled"]:
        return RedirectResponse(auth.append_query(target, login_error="飞书登录未启用"))
    if not st["configured"]:
        return RedirectResponse(
            auth.append_query(target, login_error=st["reason"] or "飞书登录未配置")
        )
    if not code:
        return RedirectResponse(auth.append_query(target, login_error="飞书未返回授权码，请重新登录"))

    try:
        user = auth.login(code, auth.redirect_uri(request))
        allowed, reason = auth.check_allowlist(user)
        if not allowed:
            print(f"[auth] 拒绝登录：{reason}")
            return RedirectResponse(auth.append_query(target, login_error=reason))
        ticket = auth.issue_ticket(user)
        print(f"[auth] 登录成功：{user.get('name')}（{user.get('email') or user.get('open_id')}）")
    except auth.AuthError as exc:
        print(f"[auth] 登录失败：{exc}")
        return RedirectResponse(auth.append_query(target, login_error=str(exc)))
    except Exception as exc:  # noqa: BLE001 - 回调页必须给出可读反馈，不能 500
        print(f"[auth] 登录回调异常：{exc}")
        return RedirectResponse(auth.append_query(target, login_error="登录失败，请稍后重试"))

    return RedirectResponse(auth.append_query(target, ticket=ticket))


@app.post("/api/auth/feishu/exchange")
async def api_feishu_exchange(payload: dict):
    """用一次性 ticket 换正式 session token。"""
    ticket = str(payload.get("ticket") or "")
    user = auth.consume_ticket(ticket)
    if not user:
        return {"success": False, "message": "登录凭证无效或已过期，请重新登录"}
    return {"success": True, "data": auth.issue_session(user)}


@app.post("/api/auth/logout")
def api_auth_logout():
    """退出登录。登录态是无状态签名 token，前端丢弃即可。

    保留这个端点是为了以后加服务端吊销/审计时前端不用改。
    """
    return {"success": True}


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
