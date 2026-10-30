"""插件本地参考录音：路径解析、有界读取与托管目录导入。"""

from pathlib import Path
from typing import Any, Dict
from uuid import uuid4

import asyncio
import re
import stat

from ..domain.models import TTSError


MAX_REFERENCE_BYTES = 7 * 1024 * 1024
REFERENCE_EXAMPLE = "reference_audio/sample.wav"


def reference_mime(data: bytes) -> str:
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"ID3") or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    raise TTSError("参考录音必须是 MP3 或 WAV 音频，不能只修改文件后缀")


class ReferenceAudioStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.directory = data_dir / "reference_audio"

    async def prepare(self) -> None:
        await asyncio.to_thread(self.directory.mkdir, parents=True, exist_ok=True)

    def _resolve(self, value: str) -> Path:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise TTSError("请填写有效的参考音频路径，例如 reference_audio/sample.wav")
        source = Path(value)
        if source.is_absolute():
            return source.resolve()
        # 相对路径只相对于 SDK 数据目录；访问目录外文件必须明确填写绝对路径。
        if source.drive or source.root:
            raise TTSError("请使用完整绝对路径，或相对于插件数据目录的路径")
        resolved = (self.data_dir / source).resolve()
        if not resolved.is_relative_to(self.data_dir.resolve()):
            raise TTSError("相对路径不能超出插件数据目录，请改用绝对路径")
        return resolved

    def _read(self, value: str, limit: int, allow_m4a: bool = False) -> bytes:
        try:
            source = self._resolve(value)
            info = source.stat()
            if not stat.S_ISREG(info.st_mode):
                raise TTSError("参考音频路径必须指向普通文件")
            if info.st_size > limit:
                raise TTSError(f"参考音频超过大小上限（{limit / 1024 / 1024:g} MiB）")
            with source.open("rb") as stream:
                data = stream.read(limit + 1)
        except FileNotFoundError:
            raise TTSError(f"参考音频不存在：{value}；相对路径以 {self.data_dir} 为基准") from None
        except (OSError, ValueError) as exc:
            raise TTSError(f"无法读取参考音频：{value}（{exc}）") from None
        if len(data) > limit:
            raise TTSError(f"参考音频超过大小上限（{limit / 1024 / 1024:g} MiB）")
        if not (allow_m4a and len(data) > 12 and data[4:8] == b"ftyp"):
            reference_mime(data)
        return data

    async def read(self, value: str, limit: int = MAX_REFERENCE_BYTES, *, allow_m4a: bool = False) -> bytes:
        return await asyncio.to_thread(self._read, value, limit, allow_m4a)

    async def local_path(self, value: str) -> str:
        """同机 GPT-SoVITS 只能接收路径；先确认录音可读，再传递绝对路径。"""

        def resolve() -> str:
            self._read(value, MAX_REFERENCE_BYTES)
            return str(self._resolve(value))

        return await asyncio.to_thread(resolve)

    def _snapshot(self) -> Dict[str, Any]:
        files = []
        for entry in self.directory.iterdir():
            if not entry.is_file() or entry.suffix.lower() not in {".wav", ".mp3", ".m4a"}:
                continue
            files.append({"path": f"reference_audio/{entry.name}", "name": entry.name, "size": entry.stat().st_size})
            if len(files) >= 100:
                break
        files.sort(key=lambda item: item["name"].casefold())
        return {
            "base_dir": str(self.data_dir),
            "directory": str(self.directory),
            "example": REFERENCE_EXAMPLE,
            "files": files,
        }

    async def snapshot(self) -> Dict[str, Any]:
        await self.prepare()
        return await asyncio.to_thread(self._snapshot)

    async def import_file(self, value: str, *, allow_m4a: bool = False) -> Dict[str, Any]:
        def copy() -> str:
            data = self._read(value, 20 * 1024 * 1024 if allow_m4a else MAX_REFERENCE_BYTES, allow_m4a)
            source = self._resolve(value)
            if source.parent == self.directory.resolve():
                return f"reference_audio/{source.name}"
            stem = re.sub(r"[^\w-]", "_", source.stem)[:40] or "voice"
            suffix = (
                ".m4a"
                if allow_m4a and data[4:8] == b"ftyp"
                else ".wav"
                if reference_mime(data) == "audio/wav"
                else ".mp3"
            )
            destination = self.directory / f"{stem}-{uuid4().hex[:12]}{suffix}"
            # 排他创建，不覆盖已有录音；导入只复制，不移动或删除原始文件。
            with destination.open("xb") as stream:
                try:
                    stream.write(data)
                except BaseException:
                    stream.close()
                    destination.unlink()
                    raise
            return f"reference_audio/{destination.name}"

        await self.prepare()
        path = await asyncio.to_thread(copy)
        return {"path": path, "library": await self.snapshot()}
