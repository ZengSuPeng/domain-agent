"""多源历史痕迹检测编排：并行查询各源并汇总「是否曾被使用」的结论。

各源的检测逻辑见 sources/ 包，每个源一个模块。

关键设计（解决「一个慢源拖垮整批请求」）：
  1. 支持 sources 过滤：调用方可只查指定源，前端因此能「每个源发一个独立请求」，
     慢源只阻塞自己那一列，不再拖累其他源。
  2. 每源独立超时预算 SOURCE_BUDGETS：到点未完成即返回 error，永不无限等待。
  3. 多域名并行：check_domains_history 用线程池并发跑各域名，避免串行累加。

当前启用的源：wayback / trustpilot / gridinsoft / virustotal。
crt.sh 已下线（限流 15s + 超时 + 退避的最坏路径超过预算，实测 5 域要 100s+，
详见 sources/crt_sh.py 顶部说明）。
"""

from concurrent.futures import ThreadPoolExecutor, wait as _wait, ALL_COMPLETED

from domain_agent.sources import (
    SOURCE_ORDER,
    check_gridinsoft,
    check_trustpilot,
    check_virustotal,
    check_wayback_source,
)
from domain_agent.sources.base import normalize, source_error, source_skipped

# 源名 → 检测函数
_SOURCE_FUNCS = {
    "wayback": check_wayback_source,
    "trustpilot": check_trustpilot,
    "gridinsoft": check_gridinsoft,
    "virustotal": check_virustotal,
}

# 每个源的整体超时上限（秒）：超过即判 error，避免慢源拖垮请求 / 阻塞事件循环。
# trustpilot 首次需冷启动 Chromium，预算放宽；wayback 的 CDX 超时后还要降级查询。
# virustotal 免费额度 4 次/分钟（15s/域名），额度只够查 7 个域名左右，超出会判超时。
SOURCE_BUDGETS = {
    "wayback": 75,
    "trustpilot": 110,
    "gridinsoft": 65,
    "virustotal": 120,
}


def check_domain_history(domain_name, sources=None):
    """多源历史痕迹检测：判断域名是否曾被真实注册使用过。

    :param domain_name: 域名
    :param sources: 可选，只检测这些源（如 ["wayback"]）。None 表示检测全部源，
                    且 VirusTotal 仅在其余源无命中时才调用（省额度）。
    :return: dict
      - status: "ok"（无源失败）/ "partial"（部分源失败）/ "error"（全部源失败）
      - ever_used: True / False / None（无法确认）
      - sources: {源名: 统一结构}
    """
    name = normalize(domain_name)
    explicit = sources is not None
    selected = [s for s in (sources or SOURCE_ORDER) if s in _SOURCE_FUNCS]

    run_vt = "virustotal" in selected
    primary = [s for s in selected if s != "virustotal"]

    results = {}
    if primary:
        results.update(_run_sources(name, primary))

    if explicit:
        # 显式指定源：要什么跑什么，不自动补 VirusTotal
        if run_vt:
            results.update(_run_sources(name, ["virustotal"]))
    else:
        # 默认全量：仅当主源都没命中时才查 VirusTotal，省免费额度
        any_hit = any(
            s.get("status") == "ok" and s.get("hit") is True
            for s in results.values()
        )
        if any_hit:
            results["virustotal"] = source_skipped(
                "virustotal", "已有其他源确认曾被使用，跳过以节省 VirusTotal 免费额度"
            )
        else:
            results.update(_run_sources(name, ["virustotal"]))

    sources_out = {key: results[key] for key in SOURCE_ORDER if key in results}
    return _summarize(name, sources_out)


def _run_sources(name, keys):
    """并发跑指定源，每个源受 SOURCE_BUDGETS 超时约束；超时返回 error。

    注意：不能用 `with ThreadPoolExecutor(...)` —— 它的 __exit__ 是
    shutdown(wait=True)，会把「等预算」变成「等所有线程真正跑完」：
    wayback 的 CDX 40s 超时后还要降级查 Availability（再 30s），最坏 70s；
    trustpilot 冷启动 Chromium 也要几秒，预算就完全失效了。
    这里手动 shutdown(wait=False)：到点立刻带着 error 返回，未完成的线程
    留在后台自然结束（各源内部都有 requests 超时兜底），不再阻塞本次响应。
    """
    results = {}
    executor = ThreadPoolExecutor(max_workers=len(keys))
    try:
        futures = {executor.submit(_SOURCE_FUNCS[k], name): k for k in keys}
        budget = max(SOURCE_BUDGETS.get(k, 60) for k in keys)
        done, not_done = _wait(
            futures.keys(), timeout=budget, return_when=ALL_COMPLETED
        )
        for fut in done:
            key = futures[fut]
            try:
                results[key] = fut.result()
            except Exception as e:
                results[key] = source_error(key, f"检测异常: {type(e).__name__}")
        for fut in not_done:
            key = futures[fut]
            results[key] = source_error(
                key, f"检测超时（{SOURCE_BUDGETS.get(key, 60)}s）"
            )
    finally:
        # cancel_futures 只取消还没开跑的；已在跑的线程由它自己的超时收敛
        executor.shutdown(wait=False, cancel_futures=True)
    return results


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


def check_domains_history(domain_list, sources=None):
    """批量多源历史痕迹检测：多域名并发执行（保持返回顺序与入参一致）。

    :param domain_list: 域名列表，如 ["xvoma.com", "abc.com"]
    :param sources: 透传给 check_domain_history
    :return: list[dict]
    """
    if isinstance(domain_list, str):
        domain_list = [domain_list]
    names = [d for d in domain_list if d]
    if not names:
        return []
    with ThreadPoolExecutor(max_workers=min(len(names), 8)) as ex:
        futures = [ex.submit(check_domain_history, d, sources) for d in names]
        return [f.result() for f in futures]
