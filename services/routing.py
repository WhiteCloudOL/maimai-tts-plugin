"""整体路由的 Provider/Model 展示、编号解析与原子切换。"""

from typing import List, Tuple

from ..domain.models import ConfiguredModel, Profile, RouteTarget, TTSError
from ..infrastructure.storage import ConfigStore


class RoutingService:
    def __init__(self, store: ConfigStore) -> None:
        self.store = store

    def choices(self) -> List[Tuple[Profile, ConfiguredModel]]:
        return [
            (profile, model) for profile in self.store.config.profiles if profile.enabled for model in profile.models
        ]

    def describe(self, page: int = 1) -> str:
        config = self.store.config
        pages: List[List[str]] = [[]]
        for index, (profile, model) in enumerate(self.choices(), 1):
            current = config.primary is not None and (config.primary.provider_id, config.primary.model_id) == (
                profile.id,
                model.id,
            )
            line = f"{index}. {profile.name}/{model.id}{'  ← 当前首选' if current else ''}"
            if sum(len(item) + 1 for item in pages[-1]) + len(line) > 1600:
                pages.append([])
            pages[-1].append(line)
        if not 1 <= page <= len(pages):
            raise TTSError(f"请选择 1–{len(pages)} 的列表页码")
        lines = [f"麦麦TTS · 模型列表（{page}/{len(pages)}）", *pages[page - 1]]
        if len(lines) == 1:
            lines.append("请在工作台添加并启用服务商，配置模型库后即可选择。")
        if len(pages) > 1:
            lines.append("翻页：/mmtts model list <页码>")
        lines.extend(["", "切换：/mmtts model 2", "或：/mmtts model <Provider>/<Model>"])
        return "\n".join(lines)

    async def switch(self, selector: str) -> str:
        if len(selector) > 500:
            raise TTSError("请使用列表中的编号或 Provider/Model")
        choices = self.choices()
        if not choices:
            raise TTSError("请先在工作台配置并启用服务商/模型")
        if selector.isdecimal():
            index = int(selector)
            if not 1 <= index <= len(choices):
                raise TTSError(f"请选择列表中 1–{len(choices)} 的编号")
            profile, model = choices[index - 1]
        else:
            matches = [
                (profile, model)
                for profile, model in choices
                if selector in {f"{profile.name}/{model.id}", f"{profile.id}/{model.id}"}
            ]
            if len(matches) != 1:
                raise TTSError("请使用列表中的完整 Provider/Model；名称重名时可使用编号")
            profile, model = matches[0]
        config = self.store.config
        candidates = ([config.primary] if config.primary is not None else []) + config.fallbacks
        selected = next(
            (target for target in candidates if (target.provider_id, target.model_id) == (profile.id, model.id)), None
        )
        if selected is None:
            selected = RouteTarget(provider_id=profile.id, model_id=model.id, voice=model.voice)
        payload = config.model_dump()
        payload["primary"] = selected.model_dump()
        payload["fallbacks"] = [
            target.model_dump()
            for target in config.fallbacks
            if (target.provider_id, target.model_id) != (profile.id, model.id)
        ]
        await self.store.save(payload, config.revision)
        return f"已切换整体首选路由：{profile.name}/{model.id}"
