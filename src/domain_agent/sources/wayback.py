"""源 1：Wayback Machine（archive.org 历史存档）。"""

import requests

from .base import format_timestamp, get_proxies, normalize, source_error, source_ok

# archive.org 从国内访问较慢，放宽超时避免误判
CDX_TIMEOUT = 40
AVAILABILITY_TIMEOUT = 30

_NO_ARCHIVE_MESSAGE = "无历史存档记录，该域名可能从未被用于建站"


def _no_archive_result(name):
    """构造「确认无存档」的结果（检测成功，只是确实没有记录）。"""
    return {
        "domain": name,
        "status": "ok",
        "has_archive": False,
        "snapshot_count": 0,
        "earliest_date": None,
        "latest_date": None,
        "archive_url": None,
        "message": _NO_ARCHIVE_MESSAGE,
    }


def _error_result(name, detail):
    """构造「检测失败」的结果（has_archive 为 None，区别于「确认无存档」）。"""
    return {
        "domain": name,
        "status": "error",
        "has_archive": None,
        "snapshot_count": 0,
        "earliest_date": None,
        "latest_date": None,
        "archive_url": None,
        "error": detail,
        "message": "检测失败，无法确认该域名是否有历史存档",
    }


def _fetch_cdx(name, match_type):
    """查一次 CDX，返回时间戳列表；接口非 200 返回 None（触发降级）。

    不传 collapse=timestamp:4：它让服务端先按年聚合再裁剪，和 matchType=domain
    （扫全部子域名）组合时实测同一次查询从 7s 涨到 31s，而绝大多数候选域名
    本来就查不到，等于每个都白付 30 秒。年份去重放到客户端做（见
    _result_from_timestamps）。代价：对存档成千上万的热门域名，limit=100 只取
    到最早的 100 条，「最近存档时间」可能偏小——但 hit（有/无存档）不受影响。

    :return: [] 表示确认无存档；None 表示接口异常、无法判断
    """
    url = "http://web.archive.org/cdx/search/cdx"
    params = {
        "url": name,
        "output": "json",
        "limit": 100,
        "fl": "timestamp",
        "matchType": match_type,
    }
    resp = requests.get(
        url, params=params, timeout=CDX_TIMEOUT, proxies=get_proxies()
    )
    if resp.status_code != 200:
        return None

    data = resp.json()
    if not isinstance(data, list) or len(data) <= 1:
        return []
    return [s[0] for s in data[1:] if len(s) >= 1 and s[0]]


def _result_from_timestamps(name, timestamps, include_sub=False):
    """把 CDX 时间戳列表整理成结果 dict。"""
    if not timestamps:
        return _no_archive_result(name)

    # 服务端不再按年 collapse，这里自己去重（同一年多条只算一年）
    year_count = len({ts[:4] for ts in timestamps if len(ts) >= 4}) or len(timestamps)
    earliest = min(timestamps)
    latest = max(timestamps)
    archive_url = f"https://web.archive.org/web/*/{name}"
    suffix = "（含子域名存档）" if include_sub else ""

    return {
        "domain": name,
        "status": "ok",
        "has_archive": True,
        "snapshot_count": year_count,
        "earliest_date": format_timestamp(earliest),
        "latest_date": format_timestamp(latest),
        "archive_url": archive_url,
        "message": (
            f"该域名在 {year_count} 个年份中有历史存档{suffix}，"
            f"最早 {format_timestamp(earliest)}，"
            f"最近 {format_timestamp(latest)}"
        ),
    }


def _check_via_cdx(name):
    """通过 CDX API 查询存档（信息完整，但热门域名可能超时）。

    先用 matchType=exact 精确匹配裸主机名（更快），查不到再退到 matchType=domain。
    只查 exact 会误判：多数站点的存档挂在 "example.com/"、"www.example.com" 之下，
    裸主机名精确匹配为空，会被当成「从未被用于建站」。

    :return: 结果 dict；若接口返回非 200 则返回 None（触发降级）
    """
    timestamps = _fetch_cdx(name, "exact")
    if timestamps is None:
        return None

    if not timestamps:
        wider = _fetch_cdx(name, "domain")
        if wider is None:
            return None
        return _result_from_timestamps(name, wider, include_sub=True)

    return _result_from_timestamps(name, timestamps)


def _check_via_availability(name):
    """通过 Availability API 查询（速度快，作为 CDX 超时的降级方案）。

    :return: 结果 dict；若接口返回非 200 则返回 None
    """
    url = "https://archive.org/wayback/available"
    params = {"url": name}
    resp = requests.get(
        url, params=params, timeout=AVAILABILITY_TIMEOUT, proxies=get_proxies()
    )
    if resp.status_code != 200:
        return None

    data = resp.json()
    snapshots = data.get("archived_snapshots", {})
    closest = snapshots.get("closest")
    archive_url = f"https://web.archive.org/web/*/{name}"

    if not closest or not closest.get("available"):
        return _no_archive_result(name)

    ts = closest.get("timestamp")
    return {
        "domain": name,
        "status": "ok",
        "has_archive": True,
        "snapshot_count": 1,
        "earliest_date": format_timestamp(ts),
        "latest_date": format_timestamp(ts),
        "archive_url": archive_url,
        "message": f"该域名存在历史存档，最近存档时间 {format_timestamp(ts)}",
    }


def check_domain_archive(domain_name):
    """查询单个域名在 Wayback Machine 的历史存档情况。

    优先使用 CDX API（信息完整），超时则降级到 Availability API。
    两者都失败时返回 status="error"，且 has_archive=None（表示「未知」而非「无」）。

    :param domain_name: 域名（不含协议前缀），如 "xvoma.com"
    :return: dict
    """
    name = normalize(domain_name)

    cdx_error = None

    # 1. 优先用 CDX API（信息完整）
    try:
        result = _check_via_cdx(name)
        if result is not None:
            return result
        cdx_error = "CDX 接口返回非 200"
    except requests.exceptions.Timeout:
        cdx_error = f"CDX 请求超时（{CDX_TIMEOUT}s）"
        print(f"[Wayback] {cdx_error}，降级到 Availability API: {name}")
    except requests.exceptions.RequestException as e:
        cdx_error = f"CDX 请求失败: {type(e).__name__}"
        print(f"[Wayback] {cdx_error}，降级到 Availability API: {name}")

    # 2. 降级：Availability API（速度快）
    try:
        result = _check_via_availability(name)
        if result is not None:
            return result
        avail_error = "Availability 接口返回非 200"
    except requests.exceptions.Timeout:
        avail_error = f"Availability 请求超时（{AVAILABILITY_TIMEOUT}s）"
    except requests.exceptions.RequestException as e:
        avail_error = f"Availability 请求失败: {type(e).__name__}"

    detail = f"{cdx_error}；{avail_error}" if cdx_error else avail_error
    print(f"[Wayback] {name} 检测失败: {detail}")
    return _error_result(name, detail)


def check_wayback_source(name):
    """把 Wayback 检测结果转成统一的多源结果结构。"""
    result = check_domain_archive(name)
    if result.get("status") != "ok":
        return source_error("wayback", result.get("error", "Wayback Machine 检测失败"))
    return source_ok(
        "wayback",
        hit=result.get("has_archive"),
        count=result.get("snapshot_count", 0),
        earliest=result.get("earliest_date"),
        latest=result.get("latest_date"),
        url=result.get("archive_url"),
        message=result.get("message", ""),
    )
