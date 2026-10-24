"""域名生成 —— DeepSeek 官方 API 版本。

与 domain.py（DMXAPI 中转站）的区别：
  1. endpoint  : https://api.deepseek.com/chat/completions
  2. model     : deepseek-flash
  3. 思考模式   : {"thinking": {"type": "enabled"}} + reasoning_effort
  4. JSON 输出  : response_format 只支持 {"type": "json_object"}（不支持 json_schema），
                 且 prompt 中必须出现 "json" 字样并给出格式样例

对外接口与 domain.py 保持一致（generate_domain_stream / generate_domain），
main.py 只需改动 import 即可切换。
"""

import json
import os
import time

import requests
import yaml

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-flash"

# 思考模式：官方默认开启，effort 默认 high
# 如需提速可把 ENABLE_THINKING 设为 False（思考模式不支持 temperature）
ENABLE_THINKING = True
# high 会消耗大量 token，容易挤占 content 配额导致回答为空；medium 平衡质量与 token
REASONING_EFFORT = "medium"

# JSON 输出上限（thinking 模式下 reasoning_content 也会占用此配额，
# 设太小会导致思考占满额度、content 为空 → 返回 0 个域名）
MAX_TOKENS = 16384


def get_api_key():
    """优先读环境变量 DEEPSEEK_API_KEY，其次读本地 config.yaml 的 keys.deepseek。"""
    env_key = os.environ.get("DEEPSEEK_API_KEY")
    if env_key:
        return env_key.strip()

    config_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(config_dir, "config.yaml")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        raise RuntimeError(
            "未找到 DeepSeek API Key：请设置环境变量 DEEPSEEK_API_KEY，"
            f"或在 {config_path} 中添加 keys.deepseek"
        )

    keys = data.get("keys") if isinstance(data, dict) else None
    if isinstance(keys, dict):
        key = keys.get("deepseek")
        if key:
            return str(key).strip()

    raise RuntimeError(
        "config.yaml 中未找到 DeepSeek API Key（需要 keys.deepseek 字段），"
        "请设置环境变量 DEEPSEEK_API_KEY"
    )


def _build_payload(theme, desc, exclude=None):
    exclude = exclude or []
    # 构建黑名单提示（仅在有黑名单时附加）
    blacklist_hint = ""
    if exclude:
        # 最多展示 50 个，避免 prompt 过长
        shown = exclude[:50]
        blacklist_hint = (
            "\n以下域名已被确认不可用（已注册/侵权风险高/曾被使用过），"
            "请绝对不要生成相同或高度相似的域名：\n"
            + ", ".join(shown)
            + ("\n" if len(exclude) > 50 else "")
        )

    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": f"""你是一个专业的域名注册与命名专家。请返回十个未被注册使用、不会侵权、没有差评的 6-8 位英文字母 .com 域名，并为每个域名提供详细的生成理由。
结合主题：{theme}，以及域名风格描述：{desc}。
创造出有意义且易读的字母组合，以大幅提高未被注册的概率。
重要：domain 字段必须是完整域名，包含 .com 后缀，例如 "xvoma.com"，而不是只返回 "xvoma"。
{blacklist_hint}
请严格以 JSON 格式输出，不要包含任何多余文本，格式样例：
{{"domains": [{{"domain": "xvoma.com", "reason": "命名理由"}}]}}
""",
            },
            {"role": "user", "content": "请给出十个符合要求的 6-8 位字母 .com 域名及理由，以 JSON 返回。"},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": MAX_TOKENS,
        "stream": True,
    }
    if ENABLE_THINKING:
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = REASONING_EFFORT
    return payload


def generate_domain_stream(theme, desc, exclude=None):
    """流式生成域名，逐个 yield 事件字典。

    事件类型：
      - {"type": "thinking", "content": "..."}   思考过程片段（增量）
      - {"type": "done", "domains": [...]}         最终域名列表
      - {"type": "error", "message": "..."}        出错

    :param exclude: 需避开的不可用域名列表
    """
    try:
        api_key = get_api_key()
    except RuntimeError as e:
        yield {"type": "error", "message": str(e)}
        return

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    data = _build_payload(theme, desc, exclude=exclude)

    content_buffer = []
    reasoning_len = 0
    # 最多尝试 2 次：应对 Clash TUN 等场景下的瞬时 DNS 解析失败
    # （getaddrinfo failed / Failed to resolve），连接成功并已开始输出后不再重试
    for attempt in range(2):
        emitted = False
        try:
            with requests.post(API_URL, headers=headers, json=data, stream=True, timeout=300) as resp:
                if resp.status_code != 200:
                    detail = resp.text[:300]
                    yield {"type": "error", "message": f"DeepSeek API 返回 {resp.status_code}：{detail}"}
                    return
                # 按字节读取并手动 UTF-8 解码，避免 requests 编码探测导致中文乱码
                for raw_line in resp.iter_lines(decode_unicode=False):
                    if not raw_line:
                        continue
                    line = raw_line.decode("utf-8", errors="replace")
                    if not line.startswith("data:"):
                        continue
                    body = line[len("data:"):].strip()
                    if body == "[DONE]":
                        break
                    try:
                        chunk = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    delta = chunk.get("choices", [{}])[0].get("delta", {}) or {}
                    reasoning = delta.get("reasoning_content") or ""
                    if reasoning:
                        reasoning_len += len(reasoning)
                        yield {"type": "thinking", "content": reasoning}
                        emitted = True
                    piece = delta.get("content") or ""
                    if piece:
                        content_buffer.append(piece)
        except requests.exceptions.ConnectionError as e:
            # 连接建立阶段失败（DNS 解析失败 / 连接被重置），且尚未输出任何内容时可重试
            if attempt == 0 and not emitted:
                content_buffer.clear()
                time.sleep(2)
                continue
            yield {"type": "error", "message": f"请求失败：{e}"}
            return
        except requests.RequestException as e:
            yield {"type": "error", "message": f"请求失败：{e}"}
            return
        break

    answer = "".join(content_buffer)
    print(f"[DeepSeek] reasoning 长度={reasoning_len}, content 长度={len(answer)}, answer 前200字={answer[:200]!r}")
    if not answer.strip():
        yield {"type": "error", "message": "DeepSeek 返回内容为空（思考过程可能耗尽了 token 配额，请重试）"}
        return
    domains = _parse_domains(answer)
    if not domains:
        yield {"type": "error", "message": f"DeepSeek 返回内容无法解析为域名列表：{answer[:200]!r}"}
        return
    yield {"type": "done", "domains": domains}


def _parse_domains(answer):
    """解析模型返回的 JSON，容错提取 domains 并补全 .com 后缀。"""
    text = (answer or "").strip()
    if not text:
        return []
    # 去掉可能的 markdown 代码块围栏
    text = text.replace("```json", "").replace("```", "").strip()
    parsed = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}") + 1
        if 0 <= start < end:
            try:
                parsed = json.loads(text[start:end])
            except json.JSONDecodeError:
                parsed = None
    if not isinstance(parsed, dict):
        return []

    domains = parsed.get("domains", [])
    if not isinstance(domains, list):
        return []

    # 补全 .com 后缀（AI 可能只返回字母部分）
    for item in domains:
        if isinstance(item, dict) and isinstance(item.get("domain"), str):
            d = item["domain"].strip().lower()
            if "." not in d:
                item["domain"] = d + ".com"
    return domains


def generate_domain(theme, desc, exclude=None):
    """非流式版本：收集流式事件，返回最终结果（兼容旧调用）。"""
    thinking_parts = []
    domains = []
    for event in generate_domain_stream(theme, desc, exclude=exclude):
        if event["type"] == "thinking":
            thinking_parts.append(event["content"])
        elif event["type"] == "done":
            domains = event["domains"]
        elif event["type"] == "error":
            return {"domains": [], "thinking": "".join(thinking_parts), "error": event["message"]}
    return {"domains": domains, "thinking": "".join(thinking_parts)}