import json
import requests

from .tool import get_api_key


def _build_payload(theme, desc):
    domain_schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "domain_response",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "domains": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "domain": {"type": "string"},
                                "reason": {"type": "string"}
                            },
                            "required": ["domain", "reason"],
                            "additionalProperties": False
                        }
                    }
                },
                "required": ["domains"],
                "additionalProperties": False
            }
        }
    }
    return {
        "model": "deepseek-v4-flash",
        "messages": [
            {
                "role": "system",
                "content": f"""你是一个专业的域名注册与命名专家。请严格按照 JSON 格式返回十个未被注册使用、不会侵权、没有差评的 6 位英文字母 .com 域名。同时，必须为每个域名提供详细的生成理由，结合主题：{theme}，以及域名风格描述：{desc}，再次强调，生成的域名不能被注册过。
                创造出有意义且易读的 6 - 8 位字母组合，以大幅提高未被注册的概率。
                重要：domain 字段必须是完整域名，包含 .com 后缀，例如 "xvoma.com"，而不是只返回 "xvoma"。
                """
            },
            {"role": "user", "content": "请给出十个符合要求的 6 - 8 位字母 .com 域名及理由。"}
        ],
        "enable_thinking": False,
        "response_format": domain_schema,
        "stream": True,
    }


def generate_domain_stream(theme, desc):
    """流式生成域名，逐个 yield 事件字典。

    事件类型：
      - {"type": "thinking", "content": "..."}   思考过程片段（增量）
      - {"type": "done", "domains": [...]}         最终域名列表
      - {"type": "error", "message": "..."}        出错
    """
    api_key = get_api_key()
    url = "https://www.dmxapi.cn/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    data = _build_payload(theme, desc)

    content_buffer = []
    try:
        with requests.post(url, headers=headers, json=data, stream=True, timeout=300) as resp:
            resp.raise_for_status()
            # 直接按字节读取并手动 UTF-8 解码，彻底避免 requests 编码探测导致的中文乱码
            for raw_line in resp.iter_lines(decode_unicode=False):
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", errors="replace")
                if line.startswith("data:"):
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    # 思考过程增量
                    reasoning = delta.get("reasoning_content") or delta.get("thinking") or ""
                    if reasoning:
                        yield {"type": "thinking", "content": reasoning}
                    # 最终内容增量
                    piece = delta.get("content") or ""
                    if piece:
                        content_buffer.append(piece)
    except requests.RequestException as e:
        yield {"type": "error", "message": f"请求失败：{e}"}
        return

    answer = "".join(content_buffer)
    try:
        parsed = json.loads(answer)
        domains = parsed.get("domains", [])
    except json.JSONDecodeError:
        domains = []
    # 补全 .com 后缀（AI 可能只返回字母部分）
    for item in domains:
        if isinstance(item, dict) and isinstance(item.get("domain"), str):
            d = item["domain"].strip().lower()
            if "." not in d:
                item["domain"] = d + ".com"
    yield {"type": "done", "domains": domains}


def generate_domain(theme, desc):
    """非流式版本：收集流式事件，返回最终结果（兼容旧调用）。"""
    thinking_parts = []
    domains = []
    for event in generate_domain_stream(theme, desc):
        if event["type"] == "thinking":
            thinking_parts.append(event["content"])
        elif event["type"] == "done":
            domains = event["domains"]
        elif event["type"] == "error":
            return {"domains": [], "thinking": "".join(thinking_parts)}
    return {"domains": domains, "thinking": "".join(thinking_parts)}