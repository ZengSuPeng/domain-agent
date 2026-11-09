"""源 5：Trustpilot（商户评价，Playwright 渲染绕过 AWS WAF）。

并发模型（为什么不是「一个页面串行查完所有域名」）：
  历史痕迹检测是「一个域名一个线程」的并行调用，每个线程都会调 check_trustpilot。
  旧实现把所有查询塞进一个页面串行跑，单域 20~30s，3 个域名就逼近
  SOURCE_BUDGETS["trustpilot"]=100s，再多必然整列判超时（明明只是想查 5 个域名）。
  现在改成「一条专属线程 + 一个事件循环 + 一个 Chromium + N 个并发页面」：
    - 同步调用方拿 concurrent.futures.Future，跨线程安全
    - 页面池由 asyncio.Queue 分发，最多 TP_PAGE_CONCURRENCY 个页面同时抓
      （别贪多，Trustpilot 的 AWS WAF 会翻脸）
    - 每个任务带 deadline：调用方早已超时放弃的任务直接丢弃，不再占用页面
    - 浏览器只启动一次（惰性），进程内所有域名查询共用
"""

import asyncio
import json
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout

try:  # 作为包的一部分被导入（后端运行时）
    from .base import HTTP_HEADERS, source_error, source_ok, source_skipped
except ImportError:  # 直接运行本文件时（uv run trustpilot.py）
    from base import HTTP_HEADERS, source_error, source_ok, source_skipped

# Trustpilot 全站由 AWS WAF 保护，普通 HTTP 请求一律返回 403（JS 挑战页），
# 因此用 Playwright 启动真实 Chromium 渲染页面，再读取内嵌的 __NEXT_DATA__。
TP_NAV_TIMEOUT = 20000        # 页面导航超时（毫秒）
# AWS WAF 的挑战页会返回 403 + "Verifying your connection..."，约 4~6s 后自己
# 跳到真实页面（实测 4.5s）。这段必须等，不能一看到 403 就判失败。
TP_CHALLENGE_WAIT = 25000     # 等挑战页跳转 + 数据节点出现的最长时间（毫秒）
TP_PER_DOMAIN_TIMEOUT = 50    # 单个域名从入队到出结果的硬上限（秒）
TP_PAGE_CONCURRENCY = 4       # 同一浏览器内并发页面数
TP_START_TIMEOUT = 90         # 首次启动 Chromium 的最长等待（秒）
# 挑战页迟迟不跳转时，最多再重载几次
TP_MAX_ATTEMPTS = 2

# 预热用的域名：只求让一次真实的挑战流程走完，把 aws-waf-token 写进上下文
WARMUP_DOMAIN = "trustpilot.com"

_tp = {
    "thread": None,
    "loop": None,
    "queue": None,
    "ready": None,  # threading.Event
    "init_error": None,
}
_tp_lock = threading.Lock()


async def _tp_enqueue(queue, job):
    await queue.put(job)


async def _tp_read_next_data(page, timeout_ms):
    """轮询等 #__NEXT_DATA__ 出现，出现即返回其文本，超时返回 None。

    不能只 wait_for_selector 一次：AWS WAF 的挑战页本身是一个「能加载成功但
    没有数据节点」的页面，挑战在后台跑完（写入 aws-waf-token cookie）后
    页面才重新渲染出真实内容。轮询能覆盖「挑战比 selector 超时更久」的情况。
    """
    end = time.monotonic() + timeout_ms / 1000
    while True:
        try:
            raw = await page.evaluate(
                "() => { const el = document.getElementById('__NEXT_DATA__');"
                " return el ? el.textContent : null; }"
            )
        except Exception:
            raw = None
        if raw:
            return raw
        if time.monotonic() >= end:
            return None
        await asyncio.sleep(0.5)


async def _tp_is_challenge(page):
    """判断当前页面是不是 AWS WAF 的人机校验页（用于给失败原因定性）。"""
    try:
        return await page.evaluate(
            "() => /awswaf|aws-waf-token|challenge-container|verify you are human/i"
            ".test(document.documentElement ? document.documentElement.innerHTML : '')"
        )
    except Exception:
        return False


async def _tp_fetch(context, name, deadline=None):
    """在浏览器上下文中打开 Trustpilot 评价页并解析商户信息（async）。

    失败时把「HTTP 状态 / 是否 WAF 挑战页 / 加载了几次 / 耗时」写进 detail，
    而不是只给一句「未取到页面数据」——否则线上排查只能靠猜。
    """
    started = time.monotonic()

    def remaining():
        return (deadline - time.monotonic()) if deadline else 10**6

    page = await context.new_page()
    try:
        resp = await page.goto(
            f"https://www.trustpilot.com/review/{name}",
            timeout=TP_NAV_TIMEOUT,
            wait_until="domcontentloaded",
        )
        status = resp.status if resp is not None else None

        # 注意：403 是 WAF 挑战页的正常状态（"Verifying your connection..."），
        # 页面会在几秒后自己跳到真实页面，所以这里不能直接判失败，只能等。
        if status == 404:
            return source_ok("trustpilot", hit=False, message="Trustpilot 无该域名的商户主页")

        def wait_ms():
            """单次等待预算：不超过 TP_CHALLENGE_WAIT，且给调用方留 5s 收结果。"""
            left = remaining()
            if left <= 5:
                return 0
            return min(TP_CHALLENGE_WAIT, int((left - 5) * 1000))

        raw = await _tp_read_next_data(page, wait_ms())
        attempts = 1

        # 还没拿到 → 挑战页可能没自己跳过去。重载一次（此时挑战 cookie 多半已写好）。
        while not raw and attempts < TP_MAX_ATTEMPTS and remaining() > 12:
            attempts += 1
            try:
                await page.reload(timeout=TP_NAV_TIMEOUT, wait_until="domcontentloaded")
            except Exception:
                pass
            raw = await _tp_read_next_data(page, wait_ms())

        if not raw:
            title = ""
            try:
                title = (await page.title()) or ""
            except Exception:
                pass
            challenge = await _tp_is_challenge(page)
            reason = "疑似 WAF 人机校验未通过" if challenge else "页面里没有数据节点"
            return source_error(
                "trustpilot",
                f"未取到页面数据（HTTP {status}，{reason}，"
                f"已加载 {attempts} 次，耗时 {time.monotonic() - started:.0f}s，"
                f"title: {title[:40]!r}）",
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
        try:
            await page.close()
        except Exception:
            pass


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


def _tp_serve(state):
    """在专属线程的事件循环里跑：启动 Chromium，喂 N 个页面工作协程，直到收到结束信号。"""

    async def _run():
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            state["init_error"] = f"playwright 未安装: {e}"
            state["ready"].set()
            return

        try:
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                user_agent=HTTP_HEADERS["User-Agent"],
                locale="en-US",
                viewport={"width": 1366, "height": 900},
            )
        except Exception as e:
            state["init_error"] = f"Chromium 启动失败: {type(e).__name__}: {e}"
            state["ready"].set()
            return

        # 循环已在运行、队列已建好，此时才允许外部线程投递任务
        queue = asyncio.Queue()
        state["loop"] = asyncio.get_running_loop()
        state["queue"] = queue
        state["init_error"] = None
        state["ready"].set()

        async def worker():
            while True:
                job = await queue.get()
                if job is None:  # 结束信号
                    return
                name, future, deadline = job
                if time.monotonic() > deadline:
                    # 调用方早就超时放弃了，别浪费一个页面；把原因写清楚，
                    # 免得将来只看到「超时」却不知道是排队还是页面慢
                    if not future.done():
                        future.set_result(source_error(
                            "trustpilot",
                            f"排队超时：等待超过 {TP_PER_DOMAIN_TIMEOUT}s 仍未轮到（页面并发上限 "
                            f"{TP_PAGE_CONCURRENCY}，同批域名太多）",
                        ))
                    continue
                try:
                    result = await _tp_fetch(context, name, deadline)
                except Exception as e:
                    result = source_error(
                        "trustpilot", f"Trustpilot 渲染异常: {type(e).__name__}: {e}"
                    )
                if not future.done():
                    future.set_result(result)

        workers = [asyncio.create_task(worker()) for _ in range(TP_PAGE_CONCURRENCY)]
        try:
            await asyncio.gather(*workers)
        finally:
            for close in (context.close, browser.close, pw.stop):
                try:
                    await close()
                except Exception:
                    pass

    try:
        asyncio.run(_run())
    except Exception as e:
        state["init_error"] = f"Trustpilot 浏览器线程异常: {type(e).__name__}: {e}"
        state["ready"].set()


def _tp_ensure_worker():
    """惰性启动专属浏览器线程（只启动一次），返回初始化错误信息（无则 None）。"""
    with _tp_lock:
        if _tp["thread"] is None:
            _tp["ready"] = threading.Event()
            thread = threading.Thread(
                target=_tp_serve,
                args=(_tp,),
                daemon=True,
                name="trustpilot-browser",
            )
            _tp["thread"] = thread
            thread.start()
        ready = _tp["ready"]

    if not ready.wait(timeout=TP_START_TIMEOUT):
        return f"Chromium 启动超时（{TP_START_TIMEOUT}s）"
    return _tp["init_error"]


def check_trustpilot(name):
    """用 Playwright 渲染 Trustpilot 评价页：该域名是否有商户主页及评价。

    有商户主页且有评价，说明该域名正在（或曾经）真实运营业务，是「曾被使用」
    的强证据。浏览器不可用（未装 playwright/Chromium）时返回 skipped。
    多个域名会由同一浏览器的多个页面并发抓取，不再排队串行。
    """
    init_error = _tp_ensure_worker()
    if init_error:
        return source_skipped("trustpilot", f"Trustpilot 浏览器不可用，已跳过（{init_error}）")

    loop = _tp["loop"]
    queue = _tp["queue"]
    if loop is None or queue is None:
        return source_error("trustpilot", "Trustpilot 浏览器未就绪")

    deadline = time.monotonic() + TP_PER_DOMAIN_TIMEOUT
    future = Future()
    try:
        asyncio.run_coroutine_threadsafe(_tp_enqueue(queue, (name, future, deadline)), loop)
    except Exception as e:
        return source_error("trustpilot", f"Trustpilot 任务投递失败: {type(e).__name__}")

    try:
        # 多给 5s 让页面把结果写完，再判超时
        return future.result(timeout=TP_PER_DOMAIN_TIMEOUT + 5)
    except FutureTimeout:
        return source_error(
            "trustpilot",
            f"检测超时（{TP_PER_DOMAIN_TIMEOUT}s 内未完成：可能是排队、"
            f"WAF 人机校验未通过，或页面加载过慢）",
        )


def warmup():
    """后台预热：启动 Chromium 并真实访问一次 Trustpilot。

    目的是让 AWS WAF 的 JS 挑战完整走一遍，把 aws-waf-token 写进浏览器上下文：
    否则服务刚启动后的第一批域名会全部卡在挑战页上（表现为「未取到页面数据」）。
    由 main.py 在启动时丢进后台线程调用，失败无副作用（后续请求仍会懒启动）。
    """
    try:
        init_error = _tp_ensure_worker()
        if init_error:
            return f"Trustpilot 预热跳过：{init_error}"
        result = check_trustpilot(WARMUP_DOMAIN)
        return f"Trustpilot 预热完成（{result.get('status')}）"
    except Exception as e:
        return f"Trustpilot 预热失败：{type(e).__name__}: {e}"


if __name__ == "__main__":
    result = check_trustpilot("skortel.com")
    print(result)
