"""多源历史痕迹检测编排：并行查询各源并汇总「是否曾被使用」的结论。

各源的检测逻辑见 sources/ 包，每个源一个模块。
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from domain_agent.sources import (
    SOURCE_ORDER,
    check_crt_sh,
    check_gridinsoft,
    check_trustpilot,
    check_virustotal,
    check_wayback_source,
)
from domain_agent.sources.base import normalize, source_error, source_skipped


def check_domain_history(domain_name):
    """多源历史痕迹检测：判断域名是否曾被真实注册使用过。

    三个主源并行查询，任一源命中即说明该域名曾被使用（注销后重新注册也算）。
    VirusTotal 免费额度仅 4 次/分钟，因此仅在前几个源都没查到痕迹时才调用。

    :return: dict
      - status: "ok"（无源失败）/ "partial"（部分源失败）/ "error"（全部源失败）
      - ever_used: True（确认曾被使用）/ False（所有源都查过且无痕迹）/ None（无法确认）
      - source_count: 命中的源数量
      - sources: {源名: 统一结构}
    """
    name = normalize(domain_name)

    tasks = {
        "wayback": check_wayback_source,
        "crt": check_crt_sh,
        "trustpilot": check_trustpilot,
        "gridinsoft": check_gridinsoft,
    }

    results = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futures = {ex.submit(fn, name): key for key, fn in tasks.items()}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                results[key] = fut.result()
            except Exception as e:
                results[key] = source_error(key, f"检测异常: {type(e).__name__}")

    if any(s["hit"] is True for s in results.values()):
        results["virustotal"] = source_skipped(
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
