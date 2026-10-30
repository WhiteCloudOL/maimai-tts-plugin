"""自定义 HTTP 协议的声明式配置；只替换变量和读取 JSON Pointer。"""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Any, Dict, List, Literal, Set
from urllib.parse import urlsplit

import json
import re


VARIABLES = {
    "text",
    "model",
    "voice",
    "voice_id",
    "speed",
    "response_format",
    "instructions",
    "sample_rate",
    "api_key",
    "reference_audio",
    "reference_base64",
    "reference_url",
    "reference_text",
}
TOKEN = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")


def template_variables(value: Any, depth: int = 0) -> Set[str]:
    if depth > 16:
        raise ValueError("请求模板最多嵌套 16 层")
    if isinstance(value, str):
        names = set(TOKEN.findall(value))
        rest = TOKEN.sub("", value)
        if "{{" in rest or "}}" in rest or names - VARIABLES:
            raise ValueError("模板变量无效，请使用工作台列出的 {{变量名}}")
        return names
    if isinstance(value, dict):
        return set().union(*(template_variables(v, depth + 1) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(template_variables(v, depth + 1) for v in value))
    return set()


class CompatibilityOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)

    mode: Literal["openai", "custom"] = "openai"
    path: str = Field(default="audio/speech", max_length=1000)
    method: Literal["POST", "GET"] = "POST"
    body_encoding: Literal["json", "form"] = "json"
    body: Dict[str, Any] = Field(
        default_factory=lambda: {
            "model": "{{model}}",
            "input": "{{text}}",
            "voice": "{{voice}}",
            "response_format": "{{response_format}}",
            "speed": "{{speed}}",
            "instructions": "{{instructions}}",
        }
    )
    headers: Dict[str, str] = Field(default_factory=dict)
    auth: Literal["bearer", "header", "none"] = "bearer"
    auth_header: str = Field(default="X-API-Key", min_length=1, max_length=100)
    auth_prefix: str = Field(default="", max_length=100)
    response: Literal["binary", "json_base64", "json_url", "json_hex", "text_url", "sse_base64"] = "binary"
    audio_pointer: str = Field(default="/data/audio", max_length=500)
    status_pointer: str = Field(default="", max_length=500)
    success_values: List[Any] = Field(default_factory=lambda: [0, 200, "ok", "success"], max_length=20)
    error_pointer: str = Field(default="/error", max_length=500)
    sse_audio_event: str = Field(default="audio", min_length=1, max_length=100)
    sse_done_event: str = Field(default="done", min_length=1, max_length=100)
    sse_type_pointer: str = Field(default="/type", max_length=500)
    models_path: str = Field(default="models", max_length=1000)
    voices_path: str = Field(default="", max_length=1000)
    models_pointer: str = Field(default="/data", max_length=500)
    model_id_pointer: str = Field(default="/id", max_length=500)
    voices_pointer: str = Field(default="/data", max_length=500)
    voice_id_pointer: str = Field(default="/id", max_length=500)
    voice_name_pointer: str = Field(default="/name", max_length=500)

    @field_validator("path", "models_path", "voices_path", mode="before")
    @classmethod
    def relative_path(cls, value: Any) -> Any:
        if isinstance(value, str) and value.startswith("/") and not value.startswith("//"):
            return value[1:]
        return value

    @model_validator(mode="after")
    def validate_protocol(self) -> "CompatibilityOptions":
        if len(json.dumps(self.model_dump(), ensure_ascii=False, allow_nan=False)) > 24000:
            raise ValueError("自定义协议配置最多 24000 字符")
        for path in (self.path, self.models_path, self.voices_path):
            if path and (
                urlsplit(path).scheme
                or path.startswith(("/", "\\"))
                or "\\" in path
                or "?" in path
                or "#" in path
                or any(part in {".", ".."} for part in path.split("/"))
                or any(c.isspace() or ord(c) < 32 for c in path)
            ):
                raise ValueError("接口路径请使用相对于 BaseURL 的路径，例如 audio/speech")
        for pointer in (
            self.audio_pointer,
            self.status_pointer,
            self.error_pointer,
            self.sse_type_pointer,
            self.models_pointer,
            self.model_id_pointer,
            self.voices_pointer,
            self.voice_id_pointer,
            self.voice_name_pointer,
        ):
            if pointer and (not pointer.startswith("/") or re.search(r"~(?![01])", pointer)):
                raise ValueError("响应字段请使用 JSON Pointer，例如 /data/audio 或 /choices/0/message/audio/data")
        for name, value in {**self.headers, self.auth_header: self.auth_prefix}.items():
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+", name) or any(
                ord(c) < 32 or ord(c) == 127 for c in value
            ):
                raise ValueError("HTTP 请求头名称或内容无效")
            if name.lower() in {"host", "content-length", "transfer-encoding", "connection", "cookie"}:
                raise ValueError("Host、连接、长度与 Cookie 请求头由传输层管理")
        template_variables(self.body)
        for value in self.headers.values():
            if template_variables(value) - {"api_key", "model", "voice_id"}:
                raise ValueError("请求头模板支持 api_key、model 和 voice_id 变量")
        for name, value in self.headers.items():
            if name.lower() in {
                "authorization",
                "api-key",
                "x-api-key",
                self.auth_header.lower(),
            } and "api_key" not in template_variables(value):
                raise ValueError("鉴权密钥请填写在平台 API Key 中，请求头通过 {{api_key}} 引用")
        if self.mode == "custom" and (not self.path or "text" not in template_variables(self.body)):
            raise ValueError("自定义请求需要填写接口路径，并在请求内容中引用 {{text}}")
        if self.mode == "custom" and (self.method == "GET" or self.body_encoding == "form"):
            if any(isinstance(value, (dict, list)) for value in self.body.values()):
                raise ValueError("GET 参数与表单字段请使用单层键值；嵌套请求请选择 JSON")
        if self.response == "sse_base64" and self.sse_audio_event == self.sse_done_event:
            raise ValueError("音频事件与完成事件需要使用不同名称")
        return self
