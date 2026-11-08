"""插件 API 的有界音频引用、分块读取和可取消任务。"""

from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic
from typing import Any, Awaitable, Callable, Dict, Optional
from uuid import uuid4

import asyncio
import base64

from ..domain.models import Audio
from ..api.models import APIError


@dataclass
class AudioEntry:
    """一次合成的音频、公开元信息和基于单调时钟的有效期。"""
    audio: Audio
    metadata: Dict[str, Any]
    expires: float


class APIAudioStore:
    """维护带容量上限和有效期的进程内音频引用池。

    资源在所属事件循环中增删，按最近访问顺序回收，分块读取按字节偏移进行。
    """
    def __init__(self) -> None:
        self.entries: OrderedDict[str, AudioEntry] = OrderedDict()
        self.size = 0

    def expire(self) -> None:
        for audio_id, entry in list(self.entries.items()):
            if entry.expires <= monotonic():
                self.size -= len(entry.audio.data)
                del self.entries[audio_id]

    def trim(self, max_bytes: int) -> None:
        """按最新容量设置回收最早访问的结果。"""
        self.expire()
        while self.entries and self.size > max_bytes:
            _, entry = self.entries.popitem(last=False)
            self.size -= len(entry.audio.data)

    def put(self, audio: Audio, metadata: Dict[str, Any], *, ttl: int, max_bytes: int) -> Dict[str, Any]:
        self.expire()
        if len(audio.data) > max_bytes:
            raise APIError("AUDIO_CAPACITY", "请提高 API 音频结果容量后合成")
        while self.entries and (self.size + len(audio.data) > max_bytes or len(self.entries) >= 256):
            _, old = self.entries.popitem(last=False)
            self.size -= len(old.audio.data)
        audio_id = "a_" + uuid4().hex
        info = dict(metadata, audio_id=audio_id, response_format=audio.response_format,
                    content_type=audio.content_type, size_bytes=len(audio.data), ttl_seconds=ttl)
        self.entries[audio_id] = AudioEntry(audio, info, monotonic() + ttl)
        self.size += len(audio.data)
        return info

    def get(self, audio_id: str) -> AudioEntry:
        self.expire()
        entry = self.entries.get(audio_id)
        if entry is None:
            raise APIError("AUDIO_EXPIRED", "请使用新合成结果中的 audio_id")
        self.entries.move_to_end(audio_id)
        return entry

    def read(self, audio_id: str, offset: int, limit: int) -> Dict[str, Any]:
        entry = self.get(audio_id)
        if type(offset) is not int or type(limit) is not int or not 0 <= offset <= len(entry.audio.data) or not 1 <= limit <= 262144:
            raise APIError("INVALID_PARAMETER", "offset 使用音频字节范围内的整数，limit 使用 1–262144")
        data = entry.audio.data[offset:offset + limit]
        next_offset = offset + len(data)
        return {**entry.metadata, "data_base64": base64.b64encode(data).decode("ascii"), "offset": offset,
                "next_offset": next_offset, "eof": next_offset == len(entry.audio.data)}

    def clear(self) -> None:
        self.entries.clear()
        self.size = 0


@dataclass
class APIJob:
    """保存任务身份、参数指纹、异步执行状态和最终 API 结果。"""
    job_id: str
    request_id: str
    fingerprint: str
    task: Optional[asyncio.Task] = None
    state: str = "running"
    result: Optional[Dict[str, Any]] = None
    expires: float = 0


class APIJobs:
    """按 request_id 复用任务，以容量、并发槽位和有效期约束任务历史。

    job_id 用于查询与取消，request_id 用于调用去重；完成结果保留到有效期结束。
    """
    def __init__(self) -> None:
        self.jobs: Dict[str, APIJob] = {}
        self.requests: Dict[str, str] = {}
        self.closed = False

    def expire(self) -> None:
        for job_id, job in list(self.jobs.items()):
            if job.state != "running" and job.expires <= monotonic():
                self.requests.pop(job.request_id, None)
                del self.jobs[job_id]

    def start(
        self, request_id: str, fingerprint: str, operation: Callable[[str], Awaitable[Dict[str, Any]]], *, limit: int, ttl: int,
    ) -> APIJob:
        self.expire()
        if self.closed:
            raise APIError("API_RELOADING", "插件正在重新加载，请使用新的调用")
        if request_id in self.requests:
            job = self.jobs[self.requests[request_id]]
            if job.fingerprint != fingerprint:
                raise APIError("REQUEST_CONFLICT", "相同 request_id 请传入相同参数")
            return job
        if sum(job.state == "running" for job in self.jobs.values()) >= limit:
            raise APIError("BUSY", "API 任务槽位已满，请稍后提交")
        if len(self.jobs) >= 128:
            completed = next((job for job in self.jobs.values() if job.state != "running"), None)
            if completed is not None:
                self.requests.pop(completed.request_id, None)
                del self.jobs[completed.job_id]
        job = APIJob("j_" + uuid4().hex, request_id, fingerprint)
        self.jobs[job.job_id] = job
        self.requests[request_id] = job.job_id

        async def run() -> Dict[str, Any]:
            try:
                job.result = await operation(job.job_id)
                job.state = "done" if job.result["ok"] else "error"
                return job.result
            except asyncio.CancelledError:
                job.state = "cancelled"
                job.result = {"ok": False, "request_id": request_id,
                              "error": {"code": "CANCELLED", "message": "任务已取消"}}
                raise
            finally:
                job.expires = monotonic() + ttl

        job.task = asyncio.create_task(run(), name="tts-api-" + job.job_id)
        return job

    async def wait(self, job: APIJob) -> Dict[str, Any]:
        """等待当前任务；请求等待者取消与任务显式取消分别处理。"""
        if job.result is not None:
            return job.result
        try:
            return await asyncio.shield(job.task)
        except asyncio.CancelledError:
            if job.state == "cancelled" and job.result is not None:
                return job.result
            raise

    def get(self, job_id: str) -> APIJob:
        self.expire()
        if job_id not in self.jobs:
            raise APIError("JOB_EXPIRED", "请使用当前提交结果中的 job_id")
        return self.jobs[job_id]

    async def close(self) -> None:
        self.closed = True
        active = [job.task for job in self.jobs.values() if job.task is not None and not job.task.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self.jobs.clear()
        self.requests.clear()
