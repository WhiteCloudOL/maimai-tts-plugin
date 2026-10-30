"""在 SDK data_dir 中原子保存工作台配置，拒绝并发覆盖与损坏数据。"""

from copy import deepcopy
from pathlib import Path
from pydantic import ValidationError
from typing import Any, Dict, Iterable, Optional

import asyncio
import json
import os
import tempfile

from ..domain.models import Profile, StudioConfig, TTSError, safe_validation_error
from .diagnostics import redact_secrets


class ConfigStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "studio.json"
        self._config = StudioConfig()
        self._lock = asyncio.Lock()

    @property
    def config(self) -> StudioConfig:
        return self._config.model_copy(deep=True)

    async def load(self) -> None:
        if await asyncio.to_thread(self.path.exists):
            # 读取或校验失败必须暴露，不能用默认配置覆盖原文件。
            try:
                contents = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
                self._config = StudioConfig.model_validate_json(contents)
            except (OSError, UnicodeError, ValidationError) as exc:
                raise TTSError(f"工作台配置读取失败：{safe_validation_error(exc)}") from None

    def snapshot(self) -> Dict[str, Any]:
        data = self._config.model_dump()
        for profile in data["profiles"]:
            profile["has_api_key"] = bool(profile.pop("api_key"))
        return data

    def profile(self, profile_id: str) -> Profile:
        for profile in self._config.profiles:
            if profile.id == profile_id:
                return profile.model_copy(deep=True)
        raise TTSError("服务商不存在，请刷新页面")

    async def save(self, payload: Dict[str, Any], revision: int) -> Dict[str, Any]:
        async with self._lock:
            if revision != self._config.revision:
                raise TTSError("配置已在其他页面更新，请刷新后重试")
            updated = self.prepare_config(payload)
            updated.revision = revision + 1
            await self._commit(updated)
            return self.snapshot()

    def prepare_profile(self, payload: Dict[str, Any]) -> Profile:
        """校验编辑中的平台并恢复未回显密钥，不要求先写入配置。"""
        data = deepcopy(payload)
        data.pop("has_api_key", None)
        old = next((profile for profile in self._config.profiles if profile.id == data.get("id")), None)
        if "api_key" not in data and old is not None:
            addresses = ("kind", "models_url", "voices_url", "clone_url")
            if old.api_key and any(data.get(field, "") != old.model_dump()[field] for field in addresses):
                raise TTSError("接口地址已更改，请重新填写密钥后操作；无需先保存")
            trusted = {old.endpoint, *(item.endpoint for item in old.models if item.endpoint)}
            trusted.update({old.base_url, *(item.base_url for item in old.models if item.base_url)})
            model_entries = data.get("models", [])
            if not isinstance(model_entries, list) or any(not isinstance(item, dict) for item in model_entries):
                raise TTSError("models 必须是模型配置列表")
            addresses_to_check = [
                data.get("endpoint", ""),
                data.get("base_url", ""),
                *(item.get("endpoint", "") for item in model_entries),
                *(item.get("base_url", "") for item in model_entries),
            ]
            if any(not isinstance(address, str) for address in addresses_to_check):
                raise TTSError("接口地址必须是字符串")
            requested = set(addresses_to_check) - {""}
            if old.api_key and requested - trusted:
                raise TTSError("模型接口地址已更改，请重新填写密钥后操作；无需先保存")
            data["api_key"] = old.api_key
        try:
            return Profile.model_validate(data)
        except ValidationError as exc:
            raise TTSError(safe_validation_error(exc)) from None

    def prepare_config(self, payload: Dict[str, Any]) -> StudioConfig:
        if not isinstance(payload.get("profiles", []), list):
            raise TTSError("profiles 必须是服务商列表")
        profiles = []
        for profile in payload.get("profiles", []):
            if not isinstance(profile, dict):
                raise TTSError("每个服务商配置必须是对象")
            profiles.append(self.prepare_profile(profile).model_dump())
        try:
            return StudioConfig.model_validate({**payload, "profiles": profiles})
        except ValidationError as exc:
            raise TTSError(safe_validation_error(exc)) from None

    async def save_profile(self, payload: Dict[str, Any], revision: int) -> Dict[str, Any]:
        """只提交当前平台；保留其他平台、回复行为与路由的已保存值。"""
        async with self._lock:
            if revision != self._config.revision:
                raise TTSError("配置已在其他页面更新，请刷新后重试")
            profile = self.prepare_profile(payload)
            data = self._config.model_dump()
            profiles = data["profiles"]
            index = next((i for i, item in enumerate(profiles) if item["id"] == profile.id), None)
            if index is None:
                profiles.append(profile.model_dump())
            else:
                profiles[index] = profile.model_dump()
            if not profile.enabled:
                data["fallbacks"] = [item for item in data["fallbacks"] if item["provider_id"] != profile.id]
                if data["primary"] and data["primary"]["provider_id"] == profile.id:
                    data["primary"] = data["fallbacks"].pop(0) if data["fallbacks"] else None
            data["revision"] = revision + 1
            try:
                updated = StudioConfig.model_validate(data)
            except ValidationError as exc:
                raise TTSError(safe_validation_error(exc)) from None
            await self._commit(updated)
            return self.snapshot()

    async def _commit(self, config: StudioConfig) -> None:
        # 线程写入不能被取消；等待原子替换落定后再释放锁，保持内存和磁盘同一版本。
        write = asyncio.create_task(asyncio.to_thread(self._write, config))
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            await write
            self._config = config
            raise
        self._config = config

    def redact(self, message: str, private_values: Iterable[str] = ()) -> str:
        values = [profile.api_key for profile in self._config.profiles]
        return redact_secrets(message, [*values, *private_values])[:1500]

    def _write(self, config: StudioConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
                temporary = stream.name
                json.dump(config.model_dump(), stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
