import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
import yaml

# 超时配置：archive.org 从国内访问较慢，放宽超时避免误判
CDX_TIMEOUT = 40
AVAILABILITY_TIMEOUT = 30

# 新增痕迹源超时（均为境外服务，本地需代理）
CC_TIMEOUT = 30
CRT_TIMEOUT = 25

# Common Crawl：从完整批次列表中采样多少个批次参与查询
# collinfo.json 按时间从新到旧排列，均匀采样即可覆盖 2008 至今
CC_INDEX_COUNT = 6
# index.commoncrawl.org 在国内会被间歇性 TLS 重置，重试次数不宜过多（最坏耗时
# 约 CC_RETRIES × CC_TIMEOUT），失败后由负缓存兜住整批检测
CC_RETRIES = 2
CC_RETRY_INTERVAL = 3
CC_MAX_WORKERS = 3

# crt.sh 稳定性较差（常返回 502），失败重试次数与间隔
CRT_RETRIES = 3
CRT_RETRY_INTERVAL = 4

# VirusTotal 免费额度为 4 次/分钟，调用间隔不得低于 15 秒
VT_TIMEOUT = 20
VT_MIN_INTERVAL = 15
_vt_state = {"last_call": 0.0}

# 多数公开接口会拒绝 python-requests 的默认 UA
# （实测 Common Crawl 不带 UA 会直接超时/断开，crt.sh 同理）
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
}

_NO_ARCHIVE_MESSAGE = "无历史存档记录，该域名可能从未被用于建站"

# 各痕迹源的展示名称
SOURCE_LABELS = {
    "wayback": "Wayback Machine",
    "crt": "证书日志 crt.sh",
    "virustotal": "VirusTotal",
}

# 源顺序（前端展示顺序）
SOURCE_ORDER = ["wayback", "crt", "virustotal"]


def _normalize(name):
    """规范化域名：去空格、转小写、无后缀时补 .com。"""
    n = str(name).strip().lower()
    if "." not in n:
        n = n + ".com"
    return n


def _get_proxies():
    """从环境变量读取代理配置。

    archive.org 在中国大陆不稳定，本地开发时可通过 ARCHIVE_PROXY 指定代理，
    例如：ARCHIVE_PROXY=http://127.0.0.1:7890
    Render 等海外服务器无需设置（直连即可）。
    """
    proxy = os.environ.get("ARCHIVE_PROXY")
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _format_timestamp(ts):
    """将 Wayback Machine 的时间戳 (YYYYMMDDHHMMSS) 格式化为 YYYY-MM-DD。"""
    if not ts:
        return None
    try:
        dt = datetime.strptime(ts, "%Y%m%d%H%M%S")
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return ts[:8] if len(ts) >= 8 else ts


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


def _check_via_cdx(name):
    """通过 CDX API 查询存档（信息完整，但热门域名可能超时）。

    :return: 结果 dict；若接口返回非 200 则返回 None（触发降级）
    """
    url = "http://web.archive.org/cdx/search/cdx"
    params = {
        "url": name,
        "output": "json",
        "limit": 100,
        "fl": "timestamp",
        "collapse": "timestamp:4",  # 同一年只取一条
    }
    resp = requests.get(
        url, params=params, timeout=CDX_TIMEOUT, proxies=_get_proxies()
    )
    if resp.status_code != 200:
        return None

    data = resp.json()
    if not isinstance(data, list) or len(data) <= 1:
        return _no_archive_result(name)

    snapshots = data[1:]
    timestamps = [s[0] for s in snapshots if len(s) >= 1 and s[0]]

    if not timestamps:
        return _no_archive_result(name)

    year_count = len(timestamps)
    earliest = min(timestamps)
    latest = max(timestamps)
    archive_url = f"https://web.archive.org/web/*/{name}"

    return {
        "domain": name,
        "status": "ok",
        "has_archive": True,
        "snapshot_count": year_count,
        "earliest_date": _format_timestamp(earliest),
        "latest_date": _format_timestamp(latest),
        "archive_url": archive_url,
        "message": (
            f"该域名在 {year_count} 个年份中有历史存档，"
            f"最早 {_format_timestamp(earliest)}，"
            f"最近 {_format_timestamp(latest)}"
        ),
    }


def _check_via_availability(name):
    """通过 Availability API 查询（速度快，作为 CDX 超时的降级方案）。

    :return: 结果 dict；若接口返回非 200 则返回 None
    """
    url = "https://archive.org/wayback/available"
    params = {"url": name}
    resp = requests.get(
        url, params=params, timeout=AVAILABILITY_TIMEOUT, proxies=_get_proxies()
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
        "earliest_date": _format_timestamp(ts),
        "latest_date": _format_timestamp(ts),
        "archive_url": archive_url,
        "message": f"该域名存在历史存档，最近存档时间 {_format_timestamp(ts)}",
    }


def check_domain_archive(domain_name):
    """查询单个域名在 Wayback Machine 的历史存档情况。

    优先使用 CDX API（信息完整），超时则降级到 Availability API。
    两者都失败时返回 status="error"，且 has_archive=None（表示「未知」而非「无」）。

    :param domain_name: 域名（不含协议前缀），如 "xvoma.com"
    :return: dict
    """
    name = _normalize(domain_name)

    cdx_error = None

    # 1. 优先用 CDX API（信息完整）
    try:
        result = _check_via_cdx(name)
        if result is not None:
            return result
        cdx_error = "CDX 接口返回非 200"
    except requests.exceptions.Timeout:
        cdx_error = f"CDX 请求超时（{CDX_TIMEOUT}s）"
        print(f"[Archive] {cdx_error}，降级到 Availability API: {name}")
    except requests.exceptions.RequestException as e:
        cdx_error = f"CDX 请求失败: {type(e).__name__}"
        print(f"[Archive] {cdx_error}，降级到 Availability API: {name}")

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
    print(f"[Archive] {name} 检测失败: {detail}")
    return _error_result(name, detail)


# ============ 多源历史痕迹检测 ============
# 统一结果结构：
# {
#   "source": "common_crawl", "label": "Common Crawl",
#   "status": "ok" | "error",     # error 表示检测失败（不等于「无痕迹」）
#   "hit": True | False | None,   # error 时为 None
#   "count": int, "earliest": str, "latest": str, "url": str,
#   "message": str, "detail": str,
# }


def _source_ok(source, hit, count=0, earliest=None, latest=None, url=None, message="", detail=""):
    return {
        "source": source,
        "label": SOURCE_LABELS.get(source, source),
        "status": "ok",
        "hit": bool(hit),
        "count": int(count or 0),
        "earliest": earliest,
        "latest": latest,
        "url": url,
        "message": message,
        "detail": detail,
    }


def _source_error(source, detail):
    return {
        "source": source,
        "label": SOURCE_LABELS.get(source, source),
        "status": "error",
        "hit": None,
        "count": 0,
        "earliest": None,
        "latest": None,
        "url": None,
        "message": "检测失败",
        "detail": detail,
    }


def _source_skipped(source, detail):
    """主动跳过（不代表无痕迹，也不影响最终判定）。"""
    return {
        "source": source,
        "label": SOURCE_LABELS.get(source, source),
        "status": "skipped",
        "hit": None,
        "count": 0,
        "earliest": None,
        "latest": None,
        "url": None,
        "message": "已跳过",
        "detail": detail,
    }


def _load_json(resp):
    """安全解析 JSON；crt.sh 等不稳定服务出错时会返回 HTML，此时返回 None。"""
    try:
        return resp.json()
    except ValueError:
        return None


# ---------- 源 1：Wayback Machine（复用已有实现） ----------
def _check_wayback_source(name):
    result = check_domain_archive(name)
    if result.get("status") != "ok":
        return _source_error("wayback", result.get("error", "Wayback Machine 检测失败"))
    return _source_ok(
        "wayback",
        hit=result.get("has_archive"),
        count=result.get("snapshot_count", 0),
        earliest=result.get("earliest_date"),
        latest=result.get("latest_date"),
        url=result.get("archive_url"),
        message=result.get("message", ""),
    )


# ---------- 源 2：Common Crawl（免密钥，2008 至今的爬虫索引） ----------
_cc_index_cache = {"ts": 0.0, "indexes": [], "error": None, "error_ts": 0.0}
CC_ERROR_CACHE_SECONDS = 60


def _sample(items, n):
    """从列表中均匀采样 n 个元素（保首保尾）。"""
    if len(items) <= n or n <= 1:
        return list(items)
    step = (len(items) - 1) / (n - 1)
    return [items[round(i * step)] for i in range(n)]


def _cc_get(url, params=None):
    """带重试的 GET：index.commoncrawl.org 的连接会被间歇性重置（TLS EOF），
    且 CDX 索引服务偶尔返回 502/504（服务端过载），均需重试。"""
    last_error = None
    for attempt in range(CC_RETRIES):
        try:
            resp = requests.get(
                url,
                params=params,
                timeout=CC_TIMEOUT,
                proxies=_get_proxies(),
                headers=HTTP_HEADERS,
            )
            if resp.status_code in (502, 504):
                last_error = RuntimeError(f"HTTP {resp.status_code}")
                if attempt < CC_RETRIES - 1:
                    time.sleep(CC_RETRY_INTERVAL)
                continue
            return resp
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < CC_RETRIES - 1:
                time.sleep(CC_RETRY_INTERVAL)
    raise last_error


def _get_cc_indexes():
    """获取 Common Crawl 的批次索引接口地址（采样后）。

    成功结果缓存 1 小时；失败结果也缓存 60 秒，避免批量检测时反复重试同一个
    被网络阻断的接口（该接口在国内会被间歇性 TLS 重置）。
    """
    now = time.time()
    if _cc_index_cache["indexes"] and now - _cc_index_cache["ts"] < 3600:
        return _cc_index_cache["indexes"]
    if _cc_index_cache["error"] and now - _cc_index_cache["error_ts"] < CC_ERROR_CACHE_SECONDS:
        raise RuntimeError(_cc_index_cache["error"])

    try:
        resp = _cc_get("https://index.commoncrawl.org/collinfo.json")
        resp.raise_for_status()
        collections = resp.json()
        if not isinstance(collections, list):
            raise ValueError("collinfo.json 格式异常")

        apis = []
        for item in collections:
            if isinstance(item, dict) and item.get("cdx-api"):
                apis.append({"id": item.get("id", ""), "api": item["cdx-api"]})
        if not apis:
            raise ValueError("collinfo.json 中未找到 cdx-api 字段")
    except Exception as e:
        detail = f"{type(e).__name__}"
        _cc_index_cache["error"] = detail
        _cc_index_cache["error_ts"] = time.time()
        raise RuntimeError(detail) from e

    sampled = _sample(apis, CC_INDEX_COUNT)
    _cc_index_cache["indexes"] = sampled
    _cc_index_cache["ts"] = now
    _cc_index_cache["error"] = None
    return sampled


def _cc_query_index(api_url, name):
    """查询单个 Common Crawl 批次；该批次无记录时返回空列表。"""
    resp = _cc_get(api_url, {"url": f"{name}/*", "output": "json", "limit": "40"})
    if resp.status_code == 404:
        return []
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")

    rows = []
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def check_common_crawl(name):
    """查询 Common Crawl 索引，判断该域名是否曾被爬取过。"""
    try:
        indexes = _get_cc_indexes()
    except Exception as e:
        return _source_error("common_crawl", f"索引列表获取失败: {e}")

    # 先用第一个批次探测：CDX 索引服务偶尔整体 504（服务端过载），
    # 此时直接返回"服务不可用"，避免浪费时间查完所有批次
    try:
        first_rows = _cc_query_index(indexes[0]["api"], name)
    except Exception as e:
        msg = str(e)
        if "HTTP 504" in msg or "HTTP 502" in msg:
            return _source_error(
                "common_crawl",
                f"CDX 索引服务暂时不可用（{e}），通常数分钟后恢复，不影响其他源",
            )
        first_rows = []

    records = list(first_rows)
    failed = []
    rest_indexes = indexes[1:]
    if rest_indexes:
        with ThreadPoolExecutor(max_workers=CC_MAX_WORKERS) as ex:
            futures = {ex.submit(_cc_query_index, idx["api"], name): idx for idx in rest_indexes}
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    records.extend(fut.result())
                except Exception as e:
                    failed.append(f"{idx['id']}({type(e).__name__})")

    if records:
        stamps = sorted(
            r.get("timestamp", "") for r in records if isinstance(r, dict) and r.get("timestamp")
        )
        return _source_ok(
            "common_crawl",
            hit=True,
            count=len(records),
            earliest=_format_timestamp(stamps[0]) if stamps else None,
            latest=_format_timestamp(stamps[-1]) if stamps else None,
            url=f"{indexes[0]['api']}?url={name}/*&output=json",
            message=f"命中 {len(records)} 条爬取记录（采样 {len(indexes)} 个批次）",
        )

    if failed:
        detail = f"{len(failed)}/{len(indexes)} 个批次查询失败，结果不完整"
        return _source_error("common_crawl", detail)

    return _source_ok("common_crawl", hit=False, message="采样批次中均无爬取记录")


# ---------- 源 3：crt.sh（证书透明度日志） ----------
def check_crt_sh(name):
    """查询 crt.sh 证书日志：签发过证书说明该域名曾真实对外提供服务。"""
    url = "https://crt.sh/"
    params = {"q": f"%.{name}", "output": "json"}

    data = None
    last_error = None
    for attempt in range(CRT_RETRIES):
        try:
            resp = requests.get(
                url,
                params=params,
                timeout=CRT_TIMEOUT,
                proxies=_get_proxies(),
                headers=HTTP_HEADERS,
            )
            if resp.status_code != 200:
                last_error = f"crt.sh 返回 HTTP {resp.status_code}"
            else:
                data = _load_json(resp)
                if data is not None:
                    break
                last_error = "crt.sh 返回内容不是有效 JSON（服务可能暂时不可用）"
        except requests.exceptions.RequestException as e:
            last_error = f"crt.sh 请求失败: {type(e).__name__}"

        if attempt < CRT_RETRIES - 1:
            time.sleep(CRT_RETRY_INTERVAL)

    if data is None:
        return _source_error("crt", last_error or "crt.sh 检测失败")

    if not isinstance(data, list):
        return _source_error("crt", "crt.sh 返回格式异常")

    if not data:
        return _source_ok("crt", hit=False, message="证书透明度日志中无该域名的证书记录")

    dates = sorted(
        str(item.get("not_before", ""))[:10]
        for item in data
        if isinstance(item, dict) and item.get("not_before")
    )
    issuers = {
        item.get("issuer_name", "")
        for item in data
        if isinstance(item, dict) and item.get("issuer_name")
    }
    return _source_ok(
        "crt",
        hit=True,
        count=len(data),
        earliest=dates[0] if dates else None,
        latest=dates[-1] if dates else None,
        url=f"https://crt.sh/?q=%25.{name}",
        message=f"签发过 {len(data)} 张证书，涉及 {len(issuers)} 个签发机构",
    )


# ---------- 源 4：VirusTotal（历史 WHOIS 创建时间 + 历史 DNS 解析） ----------
def get_virustotal_key():
    """优先读环境变量 VIRUSTOTAL_API_KEY，其次读 config.yaml 的 keys.virustotal。"""
    env_key = os.environ.get("VIRUSTOTAL_API_KEY")
    if env_key:
        return env_key.strip()

    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (FileNotFoundError, OSError):
        return None

    keys = data.get("keys") if isinstance(data, dict) else None
    if isinstance(keys, dict) and keys.get("virustotal"):
        return str(keys["virustotal"]).strip()
    return None


def _vt_wait():
    """遵守 VirusTotal 免费额度（4 次/分钟），保证两次调用间隔 >= 15 秒。"""
    elapsed = time.time() - _vt_state["last_call"]
    if elapsed < VT_MIN_INTERVAL:
        time.sleep(VT_MIN_INTERVAL - elapsed)
    _vt_state["last_call"] = time.time()


def check_virustotal(name):
    """查询 VirusTotal：历史 WHOIS 创建时间 + 历史 DNS 解析记录。

    VirusTotal 会保留域名注销前的缓存记录，因此「有创建时间」是「曾被注册」的
    强证据。未配置 API Key 时返回 skipped（不影响其他源）。
    """
    api_key = get_virustotal_key()
    if not api_key:
        return _source_skipped("virustotal", "未配置 VIRUSTOTAL_API_KEY，已跳过")

    _vt_wait()
    try:
        resp = requests.get(
            f"https://www.virustotal.com/api/v3/domains/{name}",
            headers={**HTTP_HEADERS, "x-apikey": api_key},
            timeout=VT_TIMEOUT,
            proxies=_get_proxies(),
        )
    except requests.exceptions.Timeout:
        return _source_error("virustotal", f"VirusTotal 请求超时（{VT_TIMEOUT}s）")
    except requests.exceptions.RequestException as e:
        return _source_error("virustotal", f"VirusTotal 请求失败: {type(e).__name__}")

    if resp.status_code == 404:
        return _source_ok("virustotal", hit=False, message="VirusTotal 无该域名的记录")
    if resp.status_code == 401:
        return _source_error("virustotal", "VirusTotal API Key 无效")
    if resp.status_code == 429:
        return _source_error("virustotal", "超出 VirusTotal 免费额度（4 次/分钟）")
    if resp.status_code != 200:
        return _source_error("virustotal", f"VirusTotal 返回 HTTP {resp.status_code}")

    payload = _load_json(resp)
    attrs = (((payload or {}).get("data") or {}).get("attributes")) or {}
    creation = attrs.get("creation_date")
    dns_records = attrs.get("last_dns_records") or []

    if not creation and not dns_records:
        return _source_ok("virustotal", hit=False, message="VirusTotal 无注册与解析记录")

    creation_date = None
    if creation:
        try:
            creation_date = datetime.fromtimestamp(int(creation), tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OSError, TypeError):
            creation_date = None

    parts = []
    if creation_date:
        parts.append(f"WHOIS 创建时间 {creation_date}")
    if dns_records:
        parts.append(f"{len(dns_records)} 条解析记录")

    return _source_ok(
        "virustotal",
        hit=True,
        count=len(dns_records) or 1,
        earliest=creation_date,
        url=f"https://www.virustotal.com/gui/domain/{name}",
        message="，".join(parts),
        detail=attrs.get("registrar") or "",
    )


def check_domain_history(domain_name):
    """多源历史痕迹检测：判断域名是否曾被真实注册使用过。

    三个主源并行查询，任一源命中即说明该域名曾被使用（注销后重新注册也算）。
    VirusTotal 免费额度仅 4 次/分钟，因此仅在前三个源都没查到痕迹时才调用。

    :return: dict
      - status: "ok"（无源失败）/ "partial"（部分源失败）/ "error"（全部源失败）
      - ever_used: True（确认曾被使用）/ False（所有源都查过且无痕迹）/ None（无法确认）
      - source_count: 命中的源数量
      - sources: {源名: 统一结构}
    """
    name = _normalize(domain_name)

    tasks = {
        "wayback": _check_wayback_source,
        "crt": check_crt_sh,
    }

    results = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futures = {ex.submit(fn, name): key for key, fn in tasks.items()}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                results[key] = fut.result()
            except Exception as e:
                results[key] = _source_error(key, f"检测异常: {type(e).__name__}")

    if any(s["hit"] is True for s in results.values()):
        results["virustotal"] = _source_skipped(
            "virustotal", "已有其他源确认曾被使用，跳过以节省 VirusTotal 免费额度"
        )
    else:
        results["virustotal"] = check_virustotal(name)

    sources = {key: results[key] for key in SOURCE_ORDER if key in results}
    return _summarize(name, sources)


def _summarize(name, sources):
    """汇总各源结果，给出 ever_used 与整体 status。"""
    asked = [s for s in sources.values() if s["status"] == "ok"]
    errored = [s for s in sources.values() if s["status"] == "error"]

    if any(s["hit"] is True for s in asked):
        ever_used = True
    elif errored:
        # 有源失败就不敢下「从未被使用」的结论
        ever_used = None
    elif asked and all(s["hit"] is False for s in asked):
        ever_used = False
    else:
        ever_used = None

    if not errored:
        status = "ok"
    elif len(errored) == len(sources):
        status = "error"
    else:
        status = "partial"

    return {
        "domain": name,
        "status": status,
        "ever_used": ever_used,
        "source_count": sum(1 for s in sources.values() if s["hit"] is True),
        "ok_count": len(asked),
        "total_count": len(sources),
        "sources": sources,
    }


def check_domains_history(domain_list):
    """批量多源历史痕迹检测。

    :param domain_list: 域名列表，如 ["xvoma.com", "abc.com"]
    :return: list[dict]
    """
    if isinstance(domain_list, str):
        domain_list = [domain_list]
    return [check_domain_history(d) for d in domain_list if d]
