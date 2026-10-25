"""源 6：Gridinsoft（公开信誉报告索引，免密钥直连 JSON 接口）。

Gridinsoft 有两条路：
  - 付费 Inspector API（inspector.gridinsoft.com/api/v1/domain/check，3 credits/次）
  - 官网免费公开接口（无需 key，无 WAF，普通 requests 直连即可）

本项目用免费公开接口：
  GET https://gridinsoft.com/api/domain-last-check?domain={name}
      → 已有报告时返回 {domain, id, checked_at}，无报告返回 {}
  GET https://gridinsoft.com/quick-search/domains?q={name}
      → 返回数组（含相似域名），需按 domain 精确匹配，字段含 trustscore/report_id/url
"""

import time

import requests

try:  # 作为包的一部分被导入（后端运行时）
    from .base import HTTP_HEADERS, get_proxies, load_json, source_error, source_ok
except ImportError:  # 直接运行本文件时（uv run gridinsoft.py）
    from base import HTTP_HEADERS, get_proxies, load_json, source_error, source_ok

GS_TIMEOUT = 25
GS_RETRIES = 2
GS_RETRY_INTERVAL = 3

_LAST_CHECK_URL = "https://gridinsoft.com/api/domain-last-check"
_QUICK_SEARCH_URL = "https://gridinsoft.com/quick-search/domains"
_REPORT_URL = "https://gridinsoft.com/online-virus-scanner/url/{report_id}"

# 连通性探针：选一个确定有公开报告的域名，用来区分「真的没有报告」和「接口不可达」
# （网络不通 / DNS 被劫持 / 地区限制时，接口仍可能返回 HTTP 200 + 空 {} / []，
#   跟「该域名没有报告」长得一模一样，没有探针就会把不可达误报成「无冲突」）
_CANARY_DOMAIN = "booking.com"
_CANARY_OK_TTL = 3600
_CANARY_FAIL_TTL = 300
_canary = {"ok": None, "ts": 0.0}


def _gs_get(url, params):
    """带重试的 GET；返回解析后的 JSON（失败抛 RuntimeError）。"""
    last_error = None
    for attempt in range(GS_RETRIES):
        try:
            resp = requests.get(
                url,
                params=params,
                timeout=GS_TIMEOUT,
                proxies=get_proxies(),
                headers={**HTTP_HEADERS, "Accept": "application/json"},
            )
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}"
            else:
                data = load_json(resp)
                if data is not None:
                    return data
                last_error = "返回内容不是有效 JSON"
        except requests.exceptions.Timeout:
            last_error = f"请求超时（{GS_TIMEOUT}s）"
        except requests.exceptions.RequestException as e:
            last_error = f"请求失败: {type(e).__name__}"

        if attempt < GS_RETRIES - 1:
            time.sleep(GS_RETRY_INTERVAL)

    raise RuntimeError(last_error or "请求失败")


def _exact_match(rows, name):
    """quick-search 会返回相似域名（如 booking.com 会带出 booking.com-xxx.com），
    这里只取 domain 完全相等的那条，避免把仿冒域名算到目标域名头上。"""
    if not isinstance(rows, list):
        return None
    for item in rows:
        if isinstance(item, dict) and str(item.get("domain", "")).strip().lower() == name:
            return item
    return None


def _gs_reachable():
    """探测 Gridinsoft 接口是否真的可用（带缓存，避免每个域名都多查一次）。

    :return: True（接口正常）/ False（请求不可达或返回异常）
    """
    now = time.time()
    ttl = _CANARY_OK_TTL if _canary["ok"] else _CANARY_FAIL_TTL
    if _canary["ok"] is not None and now - _canary["ts"] < ttl:
        return _canary["ok"]

    ok = False
    try:
        info = _gs_get(_LAST_CHECK_URL, {"domain": _CANARY_DOMAIN})
        ok = isinstance(info, dict) and bool(info.get("domain"))
    except RuntimeError:
        ok = False

    _canary["ok"] = ok
    _canary["ts"] = now
    return ok


def check_gridinsoft(name):
    """查询 Gridinsoft 是否已有该域名的公开信誉报告。

    有报告 = 该域名曾被公开扫描/上线过，因此作为「曾被使用」的证据。
    报告中的信任评分（0-100，越高越安全）一并返回，供参考。
    """
    try:
        last_check = _gs_get(_LAST_CHECK_URL, {"domain": name})
        search_rows = _gs_get(_QUICK_SEARCH_URL, {"q": name})
    except RuntimeError as e:
        return source_error("gridinsoft", str(e))

    matched = _exact_match(search_rows, name)

    # 返回类型不对 → 请求可能被拦截/改写，不能当作「无报告」
    if not isinstance(last_check, dict) or not isinstance(search_rows, list):
        return source_error("gridinsoft", "返回格式异常，请求可能被拦截，无法确认")

    # last-check 返回非空 dict 说明有报告；quick-search 精确命中亦可作为证据
    has_report = bool(last_check.get("domain")) or matched is not None

    if not has_report:
        # 「查不到」必须先确认接口真的可用，否则不可达会被误报成「无冲突」
        if not _gs_reachable():
            return source_error(
                "gridinsoft", "请求不可达或接口返回空数据，无法确认是否有报告"
            )
        return source_ok("gridinsoft", hit=False, message="Gridinsoft 无该域名的检测报告")

    report_id = ""
    trust_score = None
    if matched:
        report_id = matched.get("report_id") or ""
        trust_score = matched.get("trustscore")
    if not report_id and isinstance(last_check, dict):
        report_id = last_check.get("id") or ""

    checked_at = None
    if matched and matched.get("created"):
        checked_at = str(matched["created"])[:10]
    elif isinstance(last_check, dict) and last_check.get("checked_at"):
        checked_at = str(last_check["checked_at"])[:10]

    parts = ["存在公开信誉报告"]
    if trust_score is not None:
        parts.append(f"信任评分 {trust_score}")
    if checked_at:
        parts.append(f"报告时间 {checked_at}")

    return source_ok(
        "gridinsoft",
        hit=True,
        count=trust_score if isinstance(trust_score, int) else 0,
        latest=checked_at,
        url=_REPORT_URL.format(report_id=report_id) if report_id else None,
        message="，".join(parts),
        detail=report_id,
    )
if __name__ == "__main__":
    result = check_gridinsoft("snipmolo.com")
    print(result)