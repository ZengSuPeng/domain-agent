import os
import yaml


def get_api_key():
    # 优先读环境变量（Render/Vercel 部署用）
    env_key = os.environ.get("DMXAPI_KEY") or os.environ.get("API_KEY")
    if env_key:
        return env_key

    # 本地开发：从 config.yaml 读取
    config_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(config_dir, "config.yaml")

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise RuntimeError(
            f"未找到 API Key：请设置环境变量 DMXAPI_KEY，或在 {config_path} 放置 config.yaml"
        )

    if not isinstance(data, dict):
        raise RuntimeError("config.yaml 格式不正确，应为 YAML 字典")

    # 兼容多种配置结构：keys.dmxapi / keys.api_key / 顶层 api_key
    keys = data.get("keys")
    if isinstance(keys, dict):
        api_key = keys.get("dmxapi") or keys.get("api_key") or keys.get("key")
        if api_key:
            return api_key

    api_key = data.get("api_key") or data.get("key") or data.get("apikey")
    if api_key:
        return api_key

    raise RuntimeError("config.yaml 中未找到 api_key（支持 keys.dmxapi 或 api_key 字段）")
