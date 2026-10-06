"""Non-secret local configuration; credentials are never serialized."""
import json
import math
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from .common import ROOT, LabError

CONFIG_PATH = ROOT / "config/model.json"


@dataclass(frozen=True)
class ModelConfig:
    schema_version: int = 1
    provider: str = "openai-responses"
    model: str = ""
    api_key_env: str = "OPENAI_API_KEY"
    request_timeout_seconds: float = 30
    max_input_tokens: int = 60000
    max_output_tokens: int = 12000
    max_output_tokens_per_response: int = 2048
    max_network_requests: int = 24
    max_batch_input_tokens: int = 500000
    max_batch_output_tokens: int = 100000
    max_batch_model_calls: int = 600

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1 or self.provider != "openai-responses":
            raise ValueError("仅支持 OpenAI Responses API 配置 v1")
        if not isinstance(self.model, str) or len(self.model) > 120 or (self.model and not re.fullmatch(r"[A-Za-z0-9._:-]+", self.model)):
            raise ValueError("模型名称无效")
        if not isinstance(self.api_key_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", self.api_key_env):
            raise ValueError("凭据环境变量名称无效")
        timeout = self.request_timeout_seconds
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 120:
            raise ValueError("请求时限应为 1—120 秒")
        for name, value in asdict(self).items():
            if name.startswith("max_") and (type(value) is not int or not 1 <= value <= 10000000):
                raise ValueError("Token/调用预算必须为正整数且不超过 10000000")
        if not 16 <= self.max_output_tokens_per_response <= self.max_output_tokens or self.max_network_requests > 512 or self.max_batch_model_calls > 10000:
            raise ValueError("响应或请求预算超出本地限制")

    def to_dict(self):
        return asdict(self)

    def public(self):
        present = bool(os.environ.get(self.api_key_env, "").strip())
        reason = "请输入模型名称" if not self.model else "当前服务进程未读取到凭据环境变量" if not present else None
        return dict(self.to_dict(), credential_present=present, configured=reason is None, reason=reason)

    def require_ready(self):
        if not self.public()["configured"]:
            raise LabError("model_not_configured", self.public()["reason"])


def load_model_config(path=CONFIG_PATH):
    try:
        return ModelConfig(**json.loads(Path(path).read_text(encoding="utf-8")))
    except TypeError as error:
        raise ValueError("模型配置包含未知字段或无效结构") from error


def save_model_config(values, path=CONFIG_PATH):
    try:
        config = ModelConfig(**values)
    except TypeError as error:
        raise ValueError("模型配置包含未知字段或无效结构") from error
    path = Path(path)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)
    return config.public()
