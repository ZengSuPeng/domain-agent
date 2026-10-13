import os
import requests

# Cloudflare 配置文件（与本文件同目录下的 config.yaml 或项目根目录的 cloud fare config.txt）
_CONFIG_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloud_fare_config.txt"),
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "cloud fare config.txt",
    ),
]


def _get_cloudflare_config():
    """读取 Cloudflare 配置，返回 (account_id, api_token)。

    优先读环境变量 CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN，
    其次读本地配置文件。
    """
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    api_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if account_id and api_token:
        return account_id, api_token

    for path in _CONFIG_CANDIDATES:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f.readlines() if ln.strip()]
            if len(lines) >= 2:
                return lines[0], lines[1]
    raise RuntimeError(
        "未找到 Cloudflare 配置：请设置环境变量 CLOUDFLARE_ACCOUNT_ID 和 CLOUDFLARE_API_TOKEN，"
        "或在项目根目录放置 'cloud fare config.txt'（第一行 account_id，第二行 api_token）"
    )


def check_domain_availability(domain_names):
    """使用 Cloudflare Registrar API 检测域名是否可注册。

    :param domain_names: 域名列表，例如 ["gptxk.com", "abc.com"]
    :return: 字典列表，每项包含 name / registrable / reason / pricing
    """
    if isinstance(domain_names, str):
        domain_names = [domain_names]

    # 清洗域名：过滤空值/非字符串，去空白，转小写，补全 .com 后缀
    cleaned = []
    for d in domain_names:
        if isinstance(d, str) and d.strip():
            name = d.strip().lower()
            if "." not in name:
                name = name + ".com"
            cleaned.append(name)
    domain_names = cleaned
    print(f"[Cloudflare] 清洗后域名: {domain_names}")

    account_id, api_token = _get_cloudflare_config()
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/registrar/domain-check"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    payload = {"domains": domain_names}

    print(f"[Cloudflare] 请求域名: {domain_names}")
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code != 200:
        print(f"[Cloudflare] 错误响应 {resp.status_code}: {resp.text[:500]}")
    resp.raise_for_status()
    result = resp.json()

    if not result.get("success"):
        raise RuntimeError(f"Cloudflare 查询失败: {result.get('errors')}")

    domains_result = result.get("result", {}).get("domains", [])
    return [
        {
            "name": item.get("name"),
            "registrable": item.get("registrable", False),
            "reason": item.get("reason", ""),
            "pricing": item.get("pricing", {}),
        }
        for item in domains_result
    ]


def filter_registrable_domains(domain_list):
    """过滤出可注册的域名。

    :param domain_list: 域名对象列表，每项需含 "domain" 字段（如 {"domain": "xvoma.com", ...}）
    :return: 仅保留可注册域名的列表
    """
    if not domain_list:
        return []
    names = [item["domain"] for item in domain_list]
    try:
        check_results = check_domain_availability(names)
    except Exception:
        # 查询失败时不过滤，原样返回
        return domain_list

    registrable_names = {
        r["name"] for r in check_results if r.get("registrable")
    }
    return [item for item in domain_list if item["domain"] in registrable_names]
