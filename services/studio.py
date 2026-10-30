"""工作台 API：长任务立即返回 ID，试听分块读取，不占用 Host 十秒调用预算。"""

from dataclasses import dataclass, replace
from pydantic import ValidationError
from time import monotonic
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple
from uuid import uuid4

import asyncio
import base64
import json

from ..domain.catalog import resolve_model_url
from ..domain.voice_catalog import get_catalog_payload
from ..domain.intonation import TONE_LABELS, TONE_RULES
from ..domain.models import Audio, RouteTarget, StudioConfig, TTSError, safe_validation_error, validate_url
from .tts import TTSService
from ..infrastructure.storage import ConfigStore
from ..infrastructure.diagnostics import trace_scope
from ..providers.base import CloneResult
from ..providers import PROVIDERS


@dataclass
class Job:
    task: Optional[asyncio.Task] = None
    state: str = "running"
    result: Any = None
    error: str = ""
    expires_at: float = 0


class Studio:
    def __init__(self, store: ConfigStore, service: TTSService) -> None:
        self.store = store
        self.service = service
        self.jobs: Dict[str, Job] = {}

    async def close(self) -> None:
        active = [job.task for job in self.jobs.values() if job.task is not None and not job.task.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self.jobs.clear()

    async def snapshot(self) -> Dict[str, Any]:
        return {
            "config": self.store.snapshot(),
            **await asyncio.to_thread(get_catalog_payload),
            "intonation": {"labels": TONE_LABELS, "rules": TONE_RULES},
            "history": list(self.service.history),
            "cache": self.service.cache.stats(),
            "reference_audio": await self.service.http.reference_files.snapshot(),
        }

    def start(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        operation_name: str = "task",
        profile_id: str = "",
        model_id: str = "",
        private_values: Tuple[str, ...] = (),
    ) -> Dict[str, str]:
        current = monotonic()
        for job_id in list(self.jobs):
            if self.jobs[job_id].state != "running" and self.jobs[job_id].expires_at <= current:
                del self.jobs[job_id]
        if sum(job.state == "running" for job in self.jobs.values()) >= 2:
            raise TTSError("已有两个平台任务正在执行，请等待完成")
        while len(self.jobs) >= 10:
            completed = next((key for key, job in self.jobs.items() if job.state != "running"), None)
            if completed is None:
                raise TTSError("后台任务已满")
            del self.jobs[completed]
        job_id = uuid4().hex
        job = Job(expires_at=current + 600)
        self.jobs[job_id] = job

        async def run() -> None:
            with trace_scope(private_values):
                started = monotonic()
                log = self.service.log
                log.info(
                    "工作台任务开始", operation=operation_name, job_id=job_id, provider_id=profile_id, model=model_id
                )
                try:
                    job.result = await operation()
                    job.state = "done"
                    log.info(
                        "工作台任务完成",
                        operation=operation_name,
                        job_id=job_id,
                        elapsed_ms=round((monotonic() - started) * 1000),
                        item_count=len(job.result) if isinstance(job.result, list) else None,
                    )
                except asyncio.CancelledError:
                    job.state = "cancelled"
                    log.info("工作台任务取消", operation=operation_name, job_id=job_id)
                    raise
                except Exception as exc:
                    # 此处是后台任务的最终边界，异常必须被观察、记录并回传。
                    job.state = "error"
                    expected = isinstance(exc, (TTSError, ValidationError))
                    detail = (
                        safe_validation_error(exc)
                        if expected
                        else f"平台任务发生内部异常（{type(exc).__name__}），请查看带任务编号的插件日志"
                    )
                    job.error = self.store.redact(detail, private_values)
                    if expected:
                        log.error("工作台任务失败", operation=operation_name, job_id=job_id, error=job.error)
                    else:
                        log.unexpected("工作台任务内部异常", exc, operation=operation_name, job_id=job_id)
                finally:
                    job.expires_at = monotonic() + 600

        job.task = asyncio.create_task(run(), name=f"tts-studio-{job_id}")
        return {"job_id": job_id}

    def job(self, job_id: str, offset: int = 0) -> Dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None or (job.state != "running" and job.expires_at <= monotonic()):
            raise TTSError("任务已过期，请重新操作")
        result = {"state": job.state, "error": job.error}
        if isinstance(job.result, CloneResult):
            result["result"] = {"voice_id": job.result.voice_id, "state": job.result.state, "name": job.result.name}
        audio = job.result.preview if isinstance(job.result, CloneResult) else job.result
        if isinstance(audio, Audio):
            if not 0 <= offset <= len(audio.data):
                raise TTSError("音频偏移量无效")
            chunk = audio.data[offset : offset + 65536]
            result.update(
                {
                    "audio": base64.b64encode(chunk).decode("ascii"),
                    "total": len(audio.data),
                    "next_offset": offset + len(chunk),
                    "content_type": audio.content_type,
                    "format": audio.response_format,
                    "provider_id": audio.provider_id,
                }
            )
        elif not isinstance(job.result, CloneResult):
            result["result"] = job.result
        return result

    def cancel(self, job_id: str) -> Dict[str, bool]:
        job = self.jobs.get(job_id)
        if job is not None and job.task is not None and not job.task.done():
            job.state = "cancelled"
            job.task.cancel()
        return {"cancelled": True}

    def launch(
        self,
        operation: str,
        profile_id: str,
        text: str = "",
        name: str = "",
        url: str = "",
        draft: str = "",
        model_id: str = "",
        voice_id: str = "",
        refresh: bool = False,
    ) -> Dict[str, str]:
        config = None
        profile = self.store.profile(profile_id) if profile_id and not draft else None
        if draft:
            try:
                payload = json.loads(draft)
            except (ValueError, RecursionError):
                raise TTSError("当前编辑值不是有效 JSON") from None
            if not isinstance(payload, dict):
                raise TTSError("当前编辑值必须是对象")
            if "profiles" in payload:
                config = self.store.prepare_config(payload)
                profile = next((item for item in config.profiles if item.id == profile_id), None)
            else:
                profile = self.store.prepare_profile(payload)
                if profile.id != profile_id:
                    raise TTSError("编辑平台与操作目标不一致")
                preview_profile = profile.model_copy(update={"enabled": True})
                config = StudioConfig(
                    profiles=[preview_profile], primary=RouteTarget(provider_id=profile.id, model_id=profile.model)
                )
        if operation == "preview":
            if not text or len(text) > 300:
                raise TTSError("试听文本需要 1–300 字符")
            return self.start(
                lambda: self.service.synthesize(
                    text, profile_id=profile_id, model_id=model_id, config=config, refresh_cache=refresh
                ),
                operation_name=operation,
                profile_id=profile_id,
                model_id=model_id,
                private_values=(text, profile.api_key if profile else ""),
            )
        if profile is None:
            raise TTSError("请先选择服务商")
        if model_id:
            profile = profile.for_model(model_id)
        provider = self.service.provider(profile)
        if operation in {"design", "update_voice", "delete_voice"}:
            if operation == "design":
                if not name.strip() or len(name) > 80:
                    raise TTSError("请填写音色前缀")
            elif not voice_id or len(voice_id) > 500:
                raise TTSError("请填写云端音色 ID")
            if operation == "delete_voice":
                saved = self.store.config
                saved_profiles = {item.id: item for item in saved.profiles}
                routes = ([saved.primary] if saved.primary else []) + saved.fallbacks
                account = provider.deletion_parameters(profile)
                for route in routes:
                    target = saved_profiles[route.provider_id]
                    if route.model_id:
                        target = target.for_model(route.model_id)
                    target_voice = route.voice if route.voice is not None else target.voice
                    used_voices = {target_voice, *(item.voice_id for item in target.options.timbre_weights)}
                    # 以已保存的实际路由判断，覆盖同账号的其他平台配置、模型及混合音色。
                    target_provider = PROVIDERS[target.kind](self.service.http)
                    if target_provider.deletion_parameters(target) == account and voice_id in used_voices:
                        raise TTSError("整体路由正在使用此音色，请先调整路由")

            async def manage_voice() -> Any:
                try:
                    if operation == "design":
                        result = await self.service.execute(lambda: provider.design(profile, name.strip()))
                    elif operation == "update_voice":
                        result = await self.service.execute(lambda: provider.update_voice(profile, voice_id, url))
                    else:
                        await self.service.execute(lambda: provider.delete_voice(profile, voice_id))
                        return {"deleted": True, "voice_id": voice_id}
                    return replace(result, name=name.strip())
                finally:
                    self.service.invalidate_voices(profile)

            return self.start(
                manage_voice,
                operation_name=operation,
                profile_id=profile_id,
                model_id=model_id,
                private_values=(
                    text,
                    profile.api_key,
                    profile.options.voice_prompt,
                    profile.options.design_preview_text,
                ),
            )
        if operation == "models":
            return self.start(
                lambda: self.service.catalog_list(profile, "models", refresh=refresh),
                operation_name=operation,
                profile_id=profile_id,
                model_id=model_id,
                private_values=(text, profile.api_key),
            )
        if operation == "voices":
            return self.start(
                lambda: self.service.catalog_list(profile, "voices", refresh=refresh),
                operation_name=operation,
                profile_id=profile_id,
                model_id=model_id,
                private_values=(text, profile.api_key),
            )
        if operation == "clone_status":
            if not url or len(url) > 500:
                raise TTSError("请填写要查询的音色 ID")

            async def clone_status() -> Dict[str, str]:
                result = await self.service.execute(lambda: provider.clone_status(profile, url))
                self.service.invalidate_voices(profile)
                return {"voice_id": result.voice_id, "state": result.state}

            return self.start(
                clone_status,
                operation_name=operation,
                profile_id=profile_id,
                model_id=model_id,
                private_values=(text, profile.api_key),
            )
        if operation == "clone":
            if not provider.url(profile, "clone"):
                raise TTSError("请先填写该服务商的 BaseURL 或音色复刻接口")
            if not name.strip() or len(name) > 80:
                raise TTSError("请输入 1–80 字符的音色名称")
            if profile.kind == "minimax" and profile.options.minimax_clone_file_id:
                pass  # 复用平台已上传的文件 ID，创建音色时直接引用。
            elif profile.options.reference_source == "url":
                validate_url(url)
            elif profile.options.reference_source != "file":
                raise TTSError("音色复刻请选择本地文件或公开 URL")

            async def clone() -> CloneResult:
                try:
                    result = await self.service.execute(lambda: provider.clone(profile, name.strip(), url, text))
                    if isinstance(result, CloneResult):
                        return replace(result, name=name.strip())
                    return CloneResult(result, "ready", name=name.strip())
                finally:
                    self.service.invalidate_voices(profile)

            return self.start(
                clone,
                operation_name=operation,
                profile_id=profile_id,
                model_id=model_id,
                private_values=(text, profile.api_key),
            )
        raise TTSError("不支持的工作台操作")

    def resolve(self, url: str, kind: str) -> Dict[str, str]:
        return resolve_model_url(url, kind)
