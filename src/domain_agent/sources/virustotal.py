"""源 4：VirusTotal（历史 WHOIS 创建时间 + 历史 DNS 解析）。"""

import os
import time
from datetime import datetime, timezone

import requests
import yaml

from .base import HTTP_HEADERS, get_proxies, load_json, source_error, source_ok, source_skipped

VT_TIMEOUT = 20
# VirusTotal 免费额度为 4 次/分钟，调用间隔不得低于 15 秒
VT_MIN_INTERVAL = 15
_vt_state = {"last_call": 0.0}

# sources/ 的上一级才是 domain_agent/，config.yaml 在那里
_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"
)


def get_virustotal_key():
    """优先读环境变量 VIRUSTOTAL_API_KEY，其次读 config.yaml 的 keys.virustotal。"""
    env_key = os.environ.get("VIRUSTOTAL_API_KEY")
    if env_key:
        return env_key.strip()

    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
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
        return source_skipped("virustotal", "未配置 VIRUSTOTAL_API_KEY，已跳过")

    _vt_wait()
    try:
        resp = requests.get(
            f"https://www.virustotal.com/api/v3/domains/{name}",
            headers={**HTTP_HEADERS, "x-apikey": api_key},
            timeout=VT_TIMEOUT,
            proxies=get_proxies(),
        )
    except requests.exceptions.Timeout:
        return source_error("virustotal", f"VirusTotal 请求超时（{VT_TIMEOUT}s）")
    except requests.exceptions.RequestException as e:
        return source_error("virustotal", f"VirusTotal 请求失败: {type(e).__name__}")

    if resp.status_code == 404:
        return source_ok("virustotal", hit=False, message="VirusTotal 无该域名的记录")
    if resp.status_code == 401:
        return source_error("virustotal", "VirusTotal API Key 无效")
    if resp.status_code == 429:
        return source_error("virustotal", "超出 VirusTotal 免费额度（4 次/分钟）")
    if resp.status_code != 200:
        return source_error("virustotal", f"VirusTotal 返回 HTTP {resp.status_code}")

    payload = load_json(resp)
    attrs = (((payload or {}).get("data") or {}).get("attributes")) or {}
    creation = attrs.get("creation_date")
    dns_records = attrs.get("last_dns_records") or []

    if not creation and not dns_records:
        return source_ok("virustotal", hit=False, message="VirusTotal 无注册与解析记录")

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

    return source_ok(
        "virustotal",
        hit=True,
        count=len(dns_records) or 1,
        earliest=creation_date,
        url=f"https://www.virustotal.com/gui/domain/{name}",
        message="，".join(parts),
        detail=attrs.get("registrar") or "",
    )
