# -*- coding: utf-8 -*-
"""飞书（Feishu / Lark）授权登录 + 接口鉴权。

登录走标准 OAuth 2.0 授权码模式（飞书官方 v3 端点）：

  1. 浏览器打开 ``/api/auth/feishu/url`` 拿到授权链接（state 由后端 HMAC 签名，
     里面带着登录成功后要回跳的前端地址）；
  2. 跳转飞书授权页，用户确认后飞书回跳
     ``/api/auth/feishu/callback?code=...&state=...``；
  3. 后端用 code 换 ``user_access_token``，再拉 ``user_info``，校验白名单；
  4. 后端签一个 2 分钟有效的一次性 ticket，拼在前端地址后面重定向回前端；
  5. 前端用 ticket 调 ``/api/auth/feishu/exchange`` 换正式 session token，
     之后所有 ``/api/*`` 请求带 ``Authorization: Bearer <session token>``。

设计取舍：
  - session / state / ticket 都是 HMAC-SHA256 签名的无状态令牌，不落库。
    Render 重启或扩多实例都不会把已登录用户踢下线（前提 AUTH_SECRET 不变）。
  - 跨域部署（Cloudflare Pages -> Render）下第三方 Cookie 不可靠，
    所以不用 Cookie，改用 Authorization 头。
  - 未配置飞书凭证时默认关闭登录（方便本地开发）；只要配了 App ID 或
    App Secret 任意一个，就视为「想启用但没配全」→ 失败关闭（fail closed），
    所有 /api 请求返回 503，避免误以为有保护。生产环境建议再设
    AUTH_REQUIRED=1，凭证整体漏配时也同样失败关闭，而不是退化为无登录。

环境变量（详见 .env.example / README）：
  FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_SCOPE
  FEISHU_ACCOUNTS_BASE / FEISHU_API_BASE   # 国际版 Lark 可改域名
  FEISHU_REDIRECT_URI / FRONTEND_URL / PUBLIC_BASE_URL
  AUTH_SECRET / AUTH_SESSION_HOURS / AUTH_DISABLED / AUTH_REQUIRED
  AUTH_ALLOWED_EMAILS / AUTH_ALLOWED_OPEN_IDS / AUTH_ALLOWED_TENANT_KEYS
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
from typing import Any

import requests

# ============ 常量 ============

STATE_TTL = 10 * 60  # 授权 state 有效期（秒）
TICKET_TTL = 2 * 60  # 一次性 ticket 有效期（秒）
DEFAULT_SESSION_HOURS = 12
MAX_TOKEN_LENGTH = 4096
HTTP_TIMEOUT = 10

# 不需要登录就能访问的接口（登录流程本身 + 查询登录态）
PUBLIC_API_PATHS = frozenset(
    {
        "/api/auth/me",
        "/api/auth/logout",
        "/api/auth/feishu/url",
        "/api/auth/feishu/callback",
        "/api/auth/feishu/exchange",
    }
)

# 飞书 token 接口常见错误码 -> 人话（方便部署时定位问题）
_TOKEN_ERROR_HINTS = {    20001: "请求缺少必要参数",
    20002: "App ID / App Secret 不正确",
    20003: "授权码无效（可能已被使用过）",
    20004: "授权码已过期，请重新登录",
    20009: "该租户未安装这个飞书应用",
    20010: "当前飞书账号没有该应用的使用权限",
    20024: "授权码与 App ID 不匹配",
    20048: "飞书应用不存在",
    20069: "飞书应用未启用",
    20071: "回调地址与开放平台「重定向 URL」配置不一致",
}


class AuthError(Exception):
    """登录流程中可直接展示给用户的错误。"""


# ============ 配置 ============

# 兼容用户/飞书文档里常见的短键名写法（.env 里写 app_id / APP_SECRET 也能用）
_ENV_ALIASES = {
    "FEISHU_APP_ID": ("FEISHU_APPID", "APP_ID", "app_id", "appid"),
    "FEISHU_APP_SECRET": ("FEISHU_APPSECRET", "APP_SECRET", "app_secret", "appsecret"),
}


def _env(name: str) -> str:
    """读环境变量，并兼容飞书文档/常见 .env 里的短名字（APP_ID、app_secret 等）。"""
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    for alias in _ENV_ALIASES.get(name, ()):
        value = (os.environ.get(alias) or "").strip()
        if value:
            return value
    return ""


def _truthy(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


def auth_disabled() -> bool:
    """AUTH_DISABLED=1 时强制关闭登录（仅建议本地/内网使用）。"""
    return _truthy(_env("AUTH_DISABLED"))


def auth_required() -> bool:
    """AUTH_REQUIRED=1：生产环境强制启用登录。

    开启后即使凭证漏配，也按「配置不全」拒绝所有 /api 请求（503），
    而不是静默退化成无登录状态，避免生产环境裸奔。
    """
    return _truthy(_env("AUTH_REQUIRED"))


def app_id() -> str:
    return _env("FEISHU_APP_ID")


def app_secret() -> str:
    return _env("FEISHU_APP_SECRET")


def scope() -> str:
    """授权时申请的用户权限，空格分隔。

    只拿姓名/头像/open_id 不需要任何 scope；要按邮箱做白名单才需要
    contact:user.email:readonly（且要在开发者后台「权限管理」里开通）。
    """
    return _env("FEISHU_SCOPE")


def accounts_base() -> str:
    return (_env("FEISHU_ACCOUNTS_BASE") or "https://accounts.feishu.cn").rstrip("/")


def api_base() -> str:
    return (_env("FEISHU_API_BASE") or "https://open.feishu.cn").rstrip("/")


def status() -> dict:
    """当前登录配置状态（可安全返回给前端，不含密钥）。"""
    disabled = auth_disabled()
    required = auth_required()
    have_id, have_secret = bool(app_id()), bool(app_secret())
    configured = have_id and have_secret
    enabled = (not disabled) and (have_id or have_secret or required)

    reason = ""
    if disabled:
        reason = "已通过 AUTH_DISABLED 关闭登录校验"
    elif enabled and not configured:
        missing = []
        if not have_id:
            missing.append("FEISHU_APP_ID")
        if not have_secret:
            missing.append("FEISHU_APP_SECRET")
        reason = "飞书登录配置不完整：缺少 " + "、".join(missing)
        if required:
            reason += "（AUTH_REQUIRED=1，服务已按失败关闭处理）"

    return {
        "enabled": enabled,
        "configured": configured,
        "disabled": disabled,
        "required": required,
        "reason": reason,
        "allowlist_configured": bool(
            allowed_emails() or allowed_open_ids() or allowed_tenant_keys()
        ),
    }


def is_enabled() -> bool:
    return status()["enabled"]


def session_hours() -> int:
    raw = _env("AUTH_SESSION_HOURS")
    if not raw:
        return DEFAULT_SESSION_HOURS
    try:
        value = int(float(raw))
    except ValueError:
        return DEFAULT_SESSION_HOURS
    # 上限 30 天，避免把过期时间签到 100 年后
    return max(1, min(value, 24 * 30))


# ============ 签名令牌 ============


def _signing_secret() -> bytes:
    secret = _env("AUTH_SECRET")
    if secret:
        return secret.encode("utf-8")
    # 没单独配 AUTH_SECRET 时，从 App Secret 派生一个稳定密钥：
    # 这样 Render 重启不会让所有人掉线；换 App Secret 才会失效。
    shared = app_secret()
    if shared:
        return hashlib.sha256(("domain-agent/session:" + shared).encode("utf-8")).digest()
    # 走到这里说明登录被关闭了（没有凭证），给个占位密钥即可
    return b"domain-agent/disabled-auth"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _issue(payload: dict, ttl: int) -> str:
    now = int(time.time())
    body = dict(payload)
    body["iat"] = now
    body["exp"] = now + ttl
    encoded = _b64e(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signature = _b64e(hmac.new(_signing_secret(), encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def _decode(token: str, token_type: str) -> dict | None:
    """校验签名、类型与过期时间；任何异常都当成「无效」。"""
    if not token or len(token) > MAX_TOKEN_LENGTH:
        return None
    try:
        encoded, signature = token.split(".", 1)
        expected = _b64e(
            hmac.new(_signing_secret(), encoded.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_b64d(encoded))
    except (ValueError, TypeError, UnicodeError):
        return None
    except Exception:  # base64 / hmac 在极端脏输入下可能抛非 ValueError
        return None
    if not isinstance(payload, dict) or payload.get("typ") != token_type:
        return None
    try:
        if int(payload.get("exp", 0)) <= int(time.time()):
            return None
    except (TypeError, ValueError):
        return None
    return payload


# ============ state / ticket / session ============


def new_state(redirect_to: str) -> str:
    return _issue({"typ": "state", "redirect": redirect_to, "nonce": secrets.token_urlsafe(12)}, STATE_TTL)


def validate_state(token: str) -> str | None:
    payload = _decode(token, "state")
    return payload.get("redirect") if payload else None


def issue_ticket(user: dict) -> str:
    return _issue({"typ": "ticket", **_user_fields(user)}, TICKET_TTL)


def consume_ticket(ticket: str) -> dict | None:
    """校验一次性 ticket；有效则返回其中的用户信息。"""
    return _decode(ticket, "ticket")


def issue_session(user: dict) -> dict:
    ttl = session_hours() * 3600
    token = _issue({"typ": "session", **_user_fields(user)}, ttl)
    return {"token": token, "expires_at": int(time.time()) + ttl, "user": public_user(user)}


def user_from_request(request: Any) -> dict | None:
    """从 Authorization: Bearer 里还原登录用户；无效/过期返回 None。"""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    return _decode(header[7:].strip(), "session")


def _user_fields(user: dict) -> dict:
    """只挑需要的信息进签名令牌，避免把整个 user_info 塞进去。

    同时兼容飞书原始 user_info（open_id/avatar_thumb/tenant_key）和
    已签发的令牌载荷（sub/avatar/tenant），这样 ticket 换 session 时不会丢字段。
    """
    return {
        "sub": user.get("open_id") or user.get("sub") or "",
        "name": user.get("name") or user.get("en_name") or "",
        "email": user.get("email") or user.get("enterprise_email") or "",
        "avatar": user.get("avatar_thumb") or user.get("avatar_url") or user.get("avatar") or "",
        "tenant": user.get("tenant_key") or user.get("tenant") or "",
    }


def public_user(payload: dict) -> dict:
    return {
        "open_id": payload.get("sub") or payload.get("open_id") or "",
        "name": payload.get("name") or "",
        "email": payload.get("email") or "",
        "avatar": payload.get("avatar") or "",
        "tenant_key": payload.get("tenant") or payload.get("tenant_key") or "",
    }


# ============ 跳转地址与白名单 ============


def _request_base(request: Any) -> str:
    """后端对外可访问地址：优先 PUBLIC_BASE_URL，其次按请求/反代头推断。

    Render 这类平台在 TLS 终结后转发 http，request.url.scheme 可能是 http，
    导致拼出 http 回调地址与开放平台登记的 https 不一致（飞书 20071）。
    这里优先读 X-Forwarded-*，再用 FEISHU_REDIRECT_URI 显式覆盖兜底。
    """
    explicit = _env("PUBLIC_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    headers = request.headers
    scheme = (headers.get("x-forwarded-proto") or "").split(",")[0].strip() or request.url.scheme
    host = (
        (headers.get("x-forwarded-host") or "").split(",")[0].strip()
        or headers.get("host")
        or request.url.netloc
    )
    return f"{scheme}://{host}".rstrip("/")


def _allowed_origins(request: Any) -> set[str]:
    origins = {
        item.strip().rstrip("/")
        for item in _env("CORS_ORIGINS").split(",")
        if item.strip()
    }
    # 本地 Vite 开发（默认 5173）与后端同源托管页面（8001）
    origins |= {"http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8001", "http://127.0.0.1:8001"}
    if request is not None:
        origins.add(_request_base(request))
    frontend = _env("FRONTEND_URL")
    if frontend:
        origins.add(frontend.rstrip("/"))
    return origins


def safe_redirect(request: Any, candidate: str) -> str | None:
    """校验前端回跳地址，防止开放重定向（只允许已知 origin）。"""
    if not candidate:
        return None
    candidate = candidate.strip()
    try:
        parts = urllib.parse.urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin.lower() not in {o.lower() for o in _allowed_origins(request)}:
        return None
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, parts.fragment))


def default_frontend(request: Any) -> str:
    """拿不到 state 时的兜底回跳地址（例如用户直接访问回调地址）。"""
    frontend = _env("FRONTEND_URL")
    if frontend:
        return frontend.rstrip("/")
    return _request_base(request)


def append_query(url: str, **params: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    for key, value in params.items():
        if value is None:
            continue
        # 截断异常长的错误信息，避免把回调跳转的 Location 头撑爆
        text = str(value)
        query.append((key, text if len(text) <= 800 else text[:800]))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path or "/", urllib.parse.urlencode(query), parts.fragment)
    )


def redirect_uri(request: Any) -> str:
    explicit = _env("FEISHU_REDIRECT_URI")
    if explicit:
        return explicit
    return f"{_request_base(request)}/api/auth/feishu/callback"


def build_authorize_url(state: str, redirect_url: str) -> str:
    params = {
        "client_id": app_id(),
        "response_type": "code",
        "redirect_uri": redirect_url,
        "state": state,
    }
    if scope():
        params["scope"] = scope()
    return f"{accounts_base()}/open-apis/authen/v1/authorize?{urllib.parse.urlencode(params)}"


def allowed_emails() -> list[str]:
    return [item.lower() for item in _env("AUTH_ALLOWED_EMAILS").split(",") if item.strip()]


def allowed_open_ids() -> list[str]:
    return [item for item in _env("AUTH_ALLOWED_OPEN_IDS").split(",") if item.strip()]


def allowed_tenant_keys() -> list[str]:
    return [item.strip() for item in _env("AUTH_ALLOWED_TENANT_KEYS").split(",") if item.strip()]


def _email_matches(email: str, patterns: list[str]) -> bool:
    email = email.lower()
    for pattern in patterns:
        if pattern.startswith("*@"):
            domain = pattern[2:]
            if email.endswith("@" + domain):
                return True
        elif email == pattern:
            return True
    return False


def check_allowlist(user: dict) -> tuple[bool, str]:
    """返回 (是否允许登录, 不允许时的原因)。

    租户白名单是「强制门槛」：配了就只允许这些 tenant_key；
    邮箱/open_id 白名单是「可选的进一步收窄」：两者都为空则不额外限制，
    否则命中任意一个即可（应用可见范围本身仍是第一道门）。
    """
    emails = allowed_emails()
    open_ids = allowed_open_ids()
    tenants = allowed_tenant_keys()

    tenant_key = (user.get("tenant_key") or user.get("tenant") or "").strip()
    if tenants and tenant_key not in tenants:
        return False, (
            f"账号不属于允许的企业租户（tenant_key={tenant_key or '未知'}），"
            "请联系管理员开通权限。"
        )

    if not emails and not open_ids:
        return True, ""

    open_id = (user.get("open_id") or "").strip()
    email = (user.get("email") or user.get("enterprise_email") or "").strip()

    if open_ids and open_id and open_id in open_ids:
        return True, ""
    if emails and email and _email_matches(email, emails):
        return True, ""
    if emails and not email:
        return False, (
            "未获取到你的飞书邮箱，无法校验访问权限。"
            "请联系管理员在开放平台开通 contact:user.email:readonly 权限，"
            "或把 AUTH_ALLOWED_EMAILS 换成 AUTH_ALLOWED_OPEN_IDS 白名单。"
        )
    return False, f"账号 {email or open_id or '未知'} 不在系统允许名单内，请联系管理员开通权限。"


# ============ 飞书 OAuth 接口调用 ============


def _post_json(url: str, payload: dict) -> dict:
    response = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
    try:
        data = response.json()
    except ValueError:
        raise AuthError(f"飞书接口返回了非 JSON 内容（HTTP {response.status_code}），请检查接口域名 {url}")
    if not isinstance(data, dict):
        raise AuthError("飞书接口返回格式异常")
    return data


def _exchange_code(code: str, redirect_url: str) -> str:
    payload = {
        "grant_type": "authorization_code",
        "client_id": app_id(),
        "client_secret": app_secret(),
        "code": code,
        "redirect_uri": redirect_url,
    }

    # 主流程：v3 令牌端点（飞书当前推荐）。
    # 只有「网络失败 / 返回非 JSON」才回退 v2（兼容私有化旧环境）；
    # 飞书明确返回错误码时直接报错，避免同一个错误在提示里出现两遍。
    v3_error = ""
    try:
        data = _post_json(f"{accounts_base()}/oauth/v3/token", payload)
    except (requests.RequestException, AuthError) as exc:
        v3_error = f"{exc.__class__.__name__}：{exc}"
    else:
        if data.get("code") == 0 and data.get("access_token"):
            return str(data["access_token"])
        raise AuthError(_token_error_message(data))

    # 回退：v2 令牌端点
    try:
        data = _post_json(f"{api_base()}/open-apis/authen/v2/oauth/token", payload)
    except (requests.RequestException, AuthError) as exc:
        raise AuthError(
            f"飞书换取 access_token 失败：{v3_error}；v2 回退也失败（{exc.__class__.__name__}：{exc}）"
        )
    if data.get("code") == 0 and data.get("access_token"):
        return str(data["access_token"])
    raise AuthError(_token_error_message(data))


def _token_error_message(data: dict) -> str:
    code_num = data.get("code")
    hint = _TOKEN_ERROR_HINTS.get(
        code_num, data.get("error_description") or data.get("msg") or "未知错误"
    )
    return f"飞书换取 access_token 失败：{hint}（{code_num}）"


def _fetch_user_info(access_token: str) -> dict:
    url = f"{api_base()}/open-apis/authen/v1/user_info"
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=HTTP_TIMEOUT,
        )
        data = response.json()
    except requests.RequestException as exc:
        raise AuthError(f"获取飞书用户信息失败：网络异常（{exc.__class__.__name__}）")
    except ValueError:
        raise AuthError("获取飞书用户信息失败：返回内容不是 JSON，请检查网络/接口域名")

    if not isinstance(data, dict) or data.get("code") != 0:
        code_num = data.get("code") if isinstance(data, dict) else None
        message = data.get("msg") if isinstance(data, dict) else ""
        raise AuthError(f"获取飞书用户信息失败：{message or '未知错误'}（{code_num}）")

    user = data.get("data") or {}
    if not user.get("open_id"):
        raise AuthError("飞书返回的用户信息缺少 open_id，无法建立登录态")
    return user


def login(code: str, redirect_url: str) -> dict:
    """用飞书回调的 code 换回完整的 user_info。"""
    if not status()["configured"]:
        raise AuthError(status()["reason"] or "飞书登录未配置")
    if not code:
        raise AuthError("飞书未返回授权码，请重新登录")
    access_token = _exchange_code(code, redirect_url)
    return _fetch_user_info(access_token)
