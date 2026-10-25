"""历史痕迹检测源的公共定义：统一结果结构、通用工具与常量。"""

import os
from datetime import datetime

# 多数公开接口会拒绝 python-requests 的默认 UA
# （实测 Common Crawl 不带 UA 会直接超时/断开，crt.sh 同理）
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
}

# 各痕迹源的展示名称
SOURCE_LABELS = {
    "wayback": "Wayback Machine",
    "crt": "证书日志 crt.sh",
    "trustpilot": "Trustpilot 评价",
    "gridinsoft": "Gridinsoft 信誉报告",
    "virustotal": "VirusTotal",
}

# 源顺序（前端展示顺序）
SOURCE_ORDER = ["wayback", "crt", "trustpilot", "gridinsoft", "virustotal"]

# 各源统一的结果结构：
# {
#   "source": "wayback", "label": "Wayback Machine",
#   "status": "ok" | "error" | "skipped",  # error 表示检测失败（不等于「无痕迹」）
#   "hit": True | False | None,            # error / skipped 时为 None
#   "count": int, "earliest": str, "latest": str, "url": str,
#   "message": str, "detail": str,
# }


def normalize(name):
    """规范化域名：去空格、转小写、无后缀时补 .com。"""
    n = str(name).strip().lower()
    if "." not in n:
        n = n + ".com"
    return n


def get_proxies():
    """从环境变量读取代理配置。

    境外源在中国大陆不稳定，本地开发时可通过 ARCHIVE_PROXY 指定代理，
    例如：ARCHIVE_PROXY=http://127.0.0.1:7890
    Render 等海外服务器无需设置（直连即可）。
    """
    proxy = os.environ.get("ARCHIVE_PROXY")
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def format_timestamp(ts):
    """将时间戳 (YYYYMMDDHHMMSS) 格式化为 YYYY-MM-DD。"""
    if not ts:
        return None
    try:
        dt = datetime.strptime(ts, "%Y%m%d%H%M%S")
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return ts[:8] if len(ts) >= 8 else ts


def load_json(resp):
    """安全解析 JSON；crt.sh 等不稳定服务出错时会返回 HTML，此时返回 None。"""
    try:
        return resp.json()
    except ValueError:
        return None


def source_ok(source, hit, count=0, earliest=None, latest=None, url=None, message="", detail=""):
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


def source_error(source, detail):
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


def source_skipped(source, detail):
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
