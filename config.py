import os

ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY", "b561d3312b37434cbc9b2cd26d5c9211.V4PCGoVKnzffOpzu")
ZHIPU_BASE_URL = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")

GLM_MODELS = {
    "glm-5": "glm-5",
    "glm-4.7": "glm-4.7",
}

DEFAULT_MODEL = "glm-5"

DEFAULT_TEMPERATURE = 0.1
DEFAULT_MAX_TOKENS = 16384
DEFAULT_TOP_P = 0.7