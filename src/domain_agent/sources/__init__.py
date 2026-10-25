"""历史痕迹检测源：每个源一个模块，统一由 archive.py 编排。

源清单（SOURCE_ORDER 决定前端展示顺序）：
  - wayback      Wayback Machine 历史存档
  - crt          crt.sh 证书透明度日志
  - trustpilot   Trustpilot 商户评价（Playwright 渲染）
  - gridinsoft   Gridinsoft 公开信誉报告索引（免密钥 JSON 接口）
  - virustotal   VirusTotal 历史 WHOIS/DNS
另有 common_crawl（Common Crawl 爬虫索引），因服务端不稳定已从链路移除，保留备用。
"""

from .base import SOURCE_LABELS, SOURCE_ORDER
from .common_crawl import check_common_crawl
from .crt_sh import check_crt_sh
from .gridinsoft import check_gridinsoft
from .trustpilot import check_trustpilot
from .virustotal import check_virustotal
from .wayback import check_domain_archive, check_wayback_source

__all__ = [
    "SOURCE_LABELS",
    "SOURCE_ORDER",
    "check_common_crawl",
    "check_crt_sh",
    "check_gridinsoft",
    "check_trustpilot",
    "check_virustotal",
    "check_domain_archive",
    "check_wayback_source",
]
