"""全局配置"""
import os
from dotenv import load_dotenv

load_dotenv()

ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY", "")
ZHIPU_BASE_URL = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "glm-4-plus")
DEFAULT_TEMPERATURE = float(os.getenv("DEFAULT_TEMPERATURE", "0.1"))
DEFAULT_MAX_TOKENS = int(os.getenv("DEFAULT_MAX_TOKENS", "16384"))

# 支持的证据类型
EVIDENCE_TYPES = ["interventional", "causal", "mechanistic", "associational"]
