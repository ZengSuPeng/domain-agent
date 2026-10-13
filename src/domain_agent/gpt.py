import json
import os
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


def inspect(api_key, domain_data):
    """
    检测域名风险，支持传入域名信息字典
    
    Args:
        api_key: API密钥
        domain_data: 域名数据，支持两种格式：
            - 字典格式: {"domain": "xivole.com", "reason": "域名选择理由"}
            - 字符串格式: "xivole.com" (兼容旧版本)
    
    Returns:
        包含风险信息和域名选择理由的字典
    """
    # 判断传入的数据类型，提取域名和选择理由
    if isinstance(domain_data, dict):
        domain_name = domain_data.get("domain", "")
        domain_reason = domain_data.get("reason", "未提供命名理由")
    else:
        # 兼容字符串格式
        domain_name = str(domain_data)
        domain_reason = "未提供命名理由"
    
    url = "https://www.dmxapi.cn/v1/chat/completions"
    
    # 设置请求头
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    # 构造合规与侵权检测的 Prompt
    prompt = f"""
    你是一个精通全球知识产权和商标法的专家。请帮我分析以下域名是否存在严重的商标侵权、傍名牌或"排版抢注（Typosquatting）"风险：
    待测域名：{domain_name}
    
    请从以下几个维度进行判断：
    1. 是否与全球知名品牌（如 Nike, Apple, Google 等）在拼写、谐音或视觉上高度相似？
    2. 是否容易对普通消费者造成混淆？
    3. 该域名以往是否存在负面评价
    
    请严格按照以下 JSON 格式回复（不要包含多余文本）：
    {{
      "risk_level": "高/中/低",
      "target_brand": "关联的可能被侵权品牌（若无填无）",
      "risk_reason": "简短的风险分析理由",
      "negative_review": "简述以往是否存在负面评价"
    }}
    """
    
    # 设置请求体（兼容 OpenAI 格式）
    payload = {
        "model": "gpt-4o-mini",  # 或者根据你的 API 支持的模型调整
        "messages": [
            {"role": "system", "content": "你是一个严谨的知识产权风险评估助手。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        result = response.json()
        
        # 提取大模型的回复内容
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        
        # 尝试解析大模型返回的 JSON 结果
        try:
            # 清理可能存在的 markdown 代码块标记
            cleaned_content = content.replace("```json", "").replace("```", "").strip()
            risk_data = json.loads(cleaned_content)
            
            # ⭐ 将域名选择理由和风险判断理由合并到结果中
            risk_data["reason"] = domain_reason        # 域名选择理由
            # risk_data 中已有 risk_reason（风险判断理由）
            
            print(f"🔍 域名 [{domain_name}] 风险检测结果：")
            print(f"   - 风险等级: {risk_data.get('risk_level')}")
            print(f"   - 关联品牌: {risk_data.get('target_brand')}")
            print(f"   - 风险分析: {risk_data.get('risk_reason')}")
            print(f"   - 命名理由: {domain_reason}")
            return risk_data
            
        except json.JSONDecodeError:
            print(f"⚠️ 解析大模型返回的 JSON 失败，原始内容为: {content}")
            return {
                "reason": domain_reason,
                "risk_level": "未知",
                "risk_reason": content,
                "target_brand": "无",
                "negative_review": "暂无"
            }
            
    except requests.exceptions.RequestException as e:
        # 异常处理
        print(f"❌ 请求失败: {e}")
        return {
            "domain": domain_name,
            "reason": domain_reason,
            "risk_level": "未知",
            "risk_reason": f"请求失败: {e}",
            "target_brand": "无",
            "negative_review": "暂无"
        }