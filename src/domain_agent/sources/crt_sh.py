"""源 3：crt.sh（证书透明度日志）。"""

import threading
import time

import requests

from .base import HTTP_HEADERS, get_proxies, load_json, source_error, source_ok

CRT_TIMEOUT = 25
# crt.sh 稳定性较差（常返回 502/503），且对连续请求敏感：
# 上一个成功、下一个立刻失败、再下一个又成功，就是典型的高频被拒 + 后端随机过载。
# 因此拉大「任意两次请求之间」的全局最小间隔，并允许更多次退避重试。
CRT_MIN_INTERVAL = 20   # 全局最小请求间隔（秒）
CRT_RETRIES = 3         # 502 多为后端瞬时过载，多次退避重试可显著提高成功率
CRT_RETRY_BACKOFF = 25  # 失败后的额外等待（秒），叠加限流后约 25~45s 再试

# 进程内全局限流：所有线程共用同一个「上次请求时间」，
# 保证无论并发了多少个域名，crt.sh 都不会被连续敲门。
_rate_lock = threading.Lock()
_last_request_at = 0.0


def _wait_for_slot():
    """阻塞到距上次请求满 CRT_MIN_INTERVAL 秒，然后占用这个时间槽。"""
    global _last_request_at
    with _rate_lock:
        now = time.monotonic()
        wait = CRT_MIN_INTERVAL - (now - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _names_of(record):
    """取出记录里的全部主机名（name_value 可能多行，另一处是 common_name）。"""
    names = set()
    for field in ("name_value", "common_name"):
        value = record.get(field) or ""
        for line in str(value).split("\n"):
            line = line.strip().lower()
            if line:
                names.add(line)
    return names


def _belongs_to(names, name):
    """判断该证书是否真的覆盖目标域名（含子域与通配符）。"""
    return any(n == name or n.endswith("." + name) for n in names)


def check_crt_sh(name):
    """查询 crt.sh 证书日志：签发过证书说明该域名曾真实对外提供服务。"""
    url = "https://crt.sh/"
    # 用 q={name} 而不是 q=%.{name}：后者要枚举全部子域，查询重、极易 502/404；
    # 前者本身是 LIKE 匹配，返回结果同样包含子域名（如 autoconfig.example.com），
    # 再用 _belongs_to 过滤掉「只是名字里含有目标域名」的相似域名。
    params = {"q": name, "output": "json"}

    data = None
    last_error = None
    for attempt in range(CRT_RETRIES):
        _wait_for_slot()
        try:
            resp = requests.get(
                url,
                params=params,
                timeout=CRT_TIMEOUT,
                proxies=get_proxies(),
                headers=HTTP_HEADERS,
            )
            if resp.status_code != 200:
                last_error = f"crt.sh 返回 HTTP {resp.status_code}"
            else:
                data = load_json(resp)
                if data is not None:
                    break
                last_error = "crt.sh 返回内容不是有效 JSON（服务可能暂时不可用）"
        except requests.exceptions.RequestException as e:
            last_error = f"crt.sh 请求失败: {type(e).__name__}"

        if attempt < CRT_RETRIES - 1:
            time.sleep(CRT_RETRY_BACKOFF)

    if data is None:
        return source_error("crt", last_error or "crt.sh 检测失败")

    if not isinstance(data, list):
        return source_error("crt", "crt.sh 返回格式异常")

    records = [
        item
        for item in data
        if isinstance(item, dict) and _belongs_to(_names_of(item), name)
    ]

    if not records:
        message = "证书透明度日志中无该域名的证书记录"
        if data:
            message += "（检索结果均为相似域名，已排除）"
        return source_ok("crt", hit=False, message=message)

    dates = sorted(
        str(item.get("not_before", ""))[:10]
        for item in records
        if item.get("not_before")
    )
    issuers = {
        item.get("issuer_name", "")
        for item in records
        if item.get("issuer_name")
    }
    return source_ok(
        "crt",
        hit=True,
        count=len(records),
        earliest=dates[0] if dates else None,
        latest=dates[-1] if dates else None,
        url=f"https://crt.sh/?q={name}",
        message=f"签发过 {len(records)} 张证书，涉及 {len(issuers)} 个签发机构",
    )
