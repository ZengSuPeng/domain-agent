import json
import os
import time
import requests


def main(args, domain_data):
    api_key = get_api_key()
    risk_data = inspect(api_key, domain_data)
    return risk_data


def get_api_key():
    """优先读环境变量 GPT_API_KEY，其次读本地 D:\\API CONFIG.txt。"""
    env_key = os.environ.get("GPT_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if env_key:
        return env_key.strip()

    api_key_file = r"D:\API CONFIG.txt"
    try:
        with open(api_key_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        raise RuntimeError(
            "未找到 GPT API Key：请设置环境变量 GPT_API_KEY，或在 D:\\API CONFIG.txt 放置密钥"
        )


def _do_request(url, headers, payload, timeout=60, max_retries=2):
    """执行单次请求，带重试。"""
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
            return response
        except requests.exceptions.Timeout as e:
            last_error = e
            if attempt < max_retries:
                wait = 3 * (attempt + 1)
                print(f"[GPT] 超时 ({attempt + 1}/{max_retries + 1})，{wait}s 后重试")
                time.sleep(wait)
            else:
                break
        except requests.exceptions.ConnectionError as e:
            last_error = e
            if attempt < max_retries:
                wait = 2 * (attempt + 1)
                print(f"[GPT] 连接失败 ({attempt + 1}/{max_retries + 1})，{wait}s 后重试")
                time.sleep(wait)
            else:
                break
        except requests.exceptions.RequestException as e:
            # 其他请求异常不重试
            last_error = e
            break
    raise last_error if last_error else RuntimeError("未知请求错误")


def inspect(api_key, domain_data):
    """
    检测域名风险，支持传入域名信息字典
    """
    if isinstance(domain_data, dict):
        domain_name = domain_data.get("domain", "")
        domain_reason = domain_data.get("reason", "未提供命名理由")
    else:
        domain_name = str(domain_data)
        domain_reason = "未提供命名理由"

    url = "https://www.dmxapi.cn/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    prompt = f"""你是一个精通全球知识产权和商标法的专家。请帮我分析以下域名是否存在严重的商标侵权、傍名牌或Typosquatting风险：
待测域名：{domain_name}

请从以下几个维度进行判断：
1. 是否与全球知名品牌（如 Nike, Apple, Google 等）在拼写、谐音或视觉上高度相似？
2. 是否容易对普通消费者造成混淆？
3. 该域名以往是否存在负面评价

请严格按照以下 JSON 格式回复（不要包含多余文本，不要 markdown）：
{{"risk_level":"高/中/低","target_brand":"关联品牌，若无填无","risk_reason":"简短分析","negative_review":"负面评价，无则填无"}}
"""

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "你是一个严谨的知识产权风险评估助手。只输出 JSON，不要其他内容。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1
    }

    try:
        response = _do_request(url, headers, payload, timeout=60, max_retries=2)
        result = response.json()
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")

        try:
            cleaned_content = content.replace("```json", "").replace("```", "").strip()
            # 有时模型会在 JSON 前后加废话，尝试提取第一个 { 到最后一个 }
            start = cleaned_content.find("{")
            end = cleaned_content.rfind("}") + 1
            if start >= 0 and end > start:
                cleaned_content = cleaned_content[start:end]

            risk_data = json.loads(cleaned_content)
            risk_data["reason"] = domain_reason

            print(f"🔍 [{domain_name}] 风险={risk_data.get('risk_level')}, 品牌={risk_data.get('target_brand')}")
            return risk_data

        except (json.JSONDecodeError, ValueError):
            print(f"⚠️ [{domain_name}] JSON 解析失败，原始内容: {content[:200]}")
            return {
                "reason": domain_reason,
                "risk_level": "未知",
                "risk_reason": f"AI 返回格式异常：{content[:100]}",
                "target_brand": "无",
                "negative_review": "暂无"
            }

    except requests.exceptions.RequestException as e:
        print(f"❌ [{domain_name}] 请求失败（已重试）: {e}")
        return {
            "reason": domain_reason,
            "risk_level": "未知",
            "risk_reason": f"检测失败 - {type(e).__name__}: {str(e)[:80]}",
            "target_brand": "无",
            "negative_review": "暂无"
        }
