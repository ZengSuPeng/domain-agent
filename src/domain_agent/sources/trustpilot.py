"""源 5：Trustpilot（商户评价，Playwright 渲染绕过 AWS WAF）。"""

import json
import queue
import threading

try:  # 作为包的一部分被导入（后端运行时）
    from .base import HTTP_HEADERS, source_error, source_ok, source_skipped
except ImportError:  # 直接运行本文件时（uv run trustpilot.py）
    from base import HTTP_HEADERS, source_error, source_ok, source_skipped

# Trustpilot 全站由 AWS WAF 保护，普通 HTTP 请求一律返回 403（JS 挑战页），
# 因此用 Playwright 启动真实 Chromium 渲染页面，再读取内嵌的 __NEXT_DATA__。
# Playwright 同步 API 与线程绑定，而调用方在 ThreadPoolExecutor 中，故用一条
# 专属线程独占浏览器，所有查询通过队列串行投递。
TP_NAV_TIMEOUT = 25000    # 页面导航超时（毫秒）
TP_CHALLENGE_WAIT = 6000  # 过 WAF 挑战后等待数据节点出现的最长时间（毫秒）
TP_TIMEOUT = 60           # 单次查询整体超时（秒）

_tp = {"thread": None, "queue": None, "ready": None, "init_error": None}
_tp_lock = threading.Lock()


def _tp_worker(job_queue, ready_event, state):
    """专属线程：独占一个 Chromium 实例，串行处理域名查询。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        state["init_error"] = f"playwright 未安装: {e}"
        ready_event.set()
        return

    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=HTTP_HEADERS["User-Agent"],
            locale="en-US",
            viewport={"width": 1366, "height": 900},
        )
    except Exception as e:
        state["init_error"] = f"Chromium 启动失败: {type(e).__name__}: {e}"
        ready_event.set()
        return

    state["init_error"] = None
    ready_event.set()

    while True:
        job = job_queue.get()
        if job is None:
            break
        name, holder, done = job
        try:
            holder["result"] = _tp_fetch(context, name)
        except Exception as e:
            holder["result"] = source_error(
                "trustpilot", f"Trustpilot 渲染异常: {type(e).__name__}"
            )
        done.set()

    for close in (context.close, browser.close, pw.stop):
        try:
            close()
        except Exception:
            pass


def _tp_ensure_worker():
    """惰性启动专属浏览器线程（只启动一次），返回初始化错误信息（无则 None）。"""
    with _tp_lock:
        if _tp["thread"] is None:
            job_queue = queue.Queue()
            ready_event = threading.Event()
            thread = threading.Thread(
                target=_tp_worker, args=(job_queue, ready_event, _tp), daemon=True
            )
            thread.start()
            _tp["queue"] = job_queue
            _tp["ready"] = ready_event
            _tp["thread"] = thread
        ready_event = _tp["ready"]
    ready_event.wait(timeout=90)
    return _tp["init_error"]


def _tp_total_reviews(value):
    """numberOfReviews 可能是整数，也可能是 {total: n, ...} 结构。"""
    if isinstance(value, dict):
        value = value.get("total")
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _tp_parse(unit, name):
    """把 __NEXT_DATA__ 中的 businessUnit 转成统一结果结构。"""
    reviews = _tp_total_reviews(unit.get("numberOfReviews"))
    unit_id = unit.get("id") or ""
    display_name = unit.get("displayName") or ""

    parts = []
    if reviews:
        parts.append(f"{reviews} 条评价")
    for label, key in (("信任评分", "trustScore"), ("星级", "stars")):
        val = unit.get(key)
        if val is not None:
            try:
                parts.append(f"{label} {float(val):.1f}")
            except (TypeError, ValueError):
                pass
    if display_name:
        parts.append(f"商户名 {display_name}")
    if not parts:
        parts.append("存在商户主页，但暂无评价")

    return source_ok(
        "trustpilot",
        hit=bool(reviews > 0 or unit_id),
        count=reviews,
        url=f"https://www.trustpilot.com/review/{name}",
        message="，".join(parts),
        detail=unit_id,
    )


def _tp_fetch(context, name):
    """在浏览器上下文中打开 Trustpilot 评价页并解析商户信息。"""
    page = context.new_page()
    try:
        resp = page.goto(
            f"https://www.trustpilot.com/review/{name}",
            timeout=TP_NAV_TIMEOUT,
            wait_until="domcontentloaded",
        )
        if resp is not None and resp.status == 404:
            return source_ok("trustpilot", hit=False, message="Trustpilot 无该域名的商户主页")

        # WAF 挑战完成后页面才会渲染真实内容，等待数据节点出现（最多等 TP_CHALLENGE_WAIT）
        try:
            page.wait_for_selector("#__NEXT_DATA__", timeout=TP_CHALLENGE_WAIT)
        except Exception:
            pass

        raw = page.evaluate(
            "() => { const el = document.getElementById('__NEXT_DATA__');"
            " return el ? el.textContent : null; }"
        )
        if not raw:
            return source_error(
                "trustpilot", f"未取到页面数据（title: {(page.title() or '')[:40]}）"
            )

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return source_error("trustpilot", "页面数据结构异常，无法解析")

        unit = (((payload or {}).get("props") or {}).get("pageProps") or {}).get("businessUnit")
        if not isinstance(unit, dict):
            return source_ok("trustpilot", hit=False, message="Trustpilot 无该域名的商户记录")
        return _tp_parse(unit, name)
    finally:
        page.close()


def check_trustpilot(name):
    """用 Playwright 渲染 Trustpilot 评价页：该域名是否有商户主页及评价。

    有商户主页且有评价，说明该域名正在（或曾经）真实运营业务，是「曾被使用」
    的强证据。浏览器不可用（未装 playwright/Chromium）时返回 skipped。
    """
    init_error = _tp_ensure_worker()
    if init_error:
        return source_skipped("trustpilot", f"Trustpilot 浏览器不可用，已跳过（{init_error}）")

    holder = {}
    done = threading.Event()
    _tp["queue"].put((name, holder, done))
    if not done.wait(timeout=TP_TIMEOUT):
        return source_error("trustpilot", f"Trustpilot 检测超时（{TP_TIMEOUT}s）")
    return holder.get("result") or source_error("trustpilot", "Trustpilot 检测异常")

if __name__ == "__main__":
    result = check_trustpilot("snipmolo.com")
    print(result)
