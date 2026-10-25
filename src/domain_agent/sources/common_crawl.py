"""源 2：Common Crawl（免密钥，2008 至今的爬虫索引）。

注：该源已从检测链路移除（服务端 504 不稳定），代码保留备用。
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from .base import HTTP_HEADERS, format_timestamp, get_proxies, source_error, source_ok

CC_TIMEOUT = 30

# 从完整批次列表中采样多少个批次参与查询
# collinfo.json 按时间从新到旧排列，均匀采样即可覆盖 2008 至今
CC_INDEX_COUNT = 6
# index.commoncrawl.org 在国内会被间歇性 TLS 重置，重试次数不宜过多（最坏耗时
# 约 CC_RETRIES × CC_TIMEOUT），失败后由负缓存兜住整批检测
CC_RETRIES = 2
CC_RETRY_INTERVAL = 3
CC_MAX_WORKERS = 3
CC_ERROR_CACHE_SECONDS = 60

_cc_index_cache = {"ts": 0.0, "indexes": [], "error": None, "error_ts": 0.0}


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
                proxies=get_proxies(),
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
        return source_error("common_crawl", f"索引列表获取失败: {e}")

    # 先用第一个批次探测：CDX 索引服务偶尔整体 504（服务端过载），
    # 此时直接返回"服务不可用"，避免浪费时间查完所有批次
    try:
        first_rows = _cc_query_index(indexes[0]["api"], name)
    except Exception as e:
        msg = str(e)
        if "HTTP 504" in msg or "HTTP 502" in msg:
            return source_error(
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
        return source_ok(
            "common_crawl",
            hit=True,
            count=len(records),
            earliest=format_timestamp(stamps[0]) if stamps else None,
            latest=format_timestamp(stamps[-1]) if stamps else None,
            url=f"{indexes[0]['api']}?url={name}/*&output=json",
            message=f"命中 {len(records)} 条爬取记录（采样 {len(indexes)} 个批次）",
        )

    if failed:
        detail = f"{len(failed)}/{len(indexes)} 个批次查询失败，结果不完整"
        return source_error("common_crawl", detail)

    return source_ok("common_crawl", hit=False, message="采样批次中均无爬取记录")
