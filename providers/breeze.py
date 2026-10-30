"""同一 Breeze 服务商按平台连接方式分发，自托管与在线协议分别维护。"""

from typing import Any, Dict, List, Union

from ..domain.models import Audio, Profile
from .base import CloneResult, Provider
from .breeze_cloud import BreezeCloudProvider
from .breeze_local import BreezeLocalProvider


class BreezeProvider(Provider):
    def implementation(self, profile: Profile) -> Provider:
        return (BreezeCloudProvider if profile.options.breeze_api_mode == "cloud" else BreezeLocalProvider)(self.http)

    def url(self, profile: Profile, operation: str = "speech", *, websocket: bool = False, model: str = "") -> str:
        return self.implementation(profile).url(profile, operation, websocket=websocket, model=model)

    def list_cache_parameters(self, profile: Profile, operation: str) -> Dict[str, Any]:
        return {
            **self.implementation(profile).list_cache_parameters(profile, operation),
            "api_mode": profile.options.breeze_api_mode,
        }

    def deletion_parameters(self, profile: Profile) -> Dict[str, Any]:
        return self.implementation(profile).deletion_parameters(profile)

    async def synthesize(self, profile: Profile, text: str) -> Audio:
        return await self.implementation(profile).synthesize(profile, text)

    async def models(self, profile: Profile) -> List[str]:
        return await self.implementation(profile).models(profile)

    async def voices(self, profile: Profile) -> List[Dict[str, str]]:
        return await self.implementation(profile).voices(profile)

    async def clone(self, profile: Profile, name: str, url: str, text: str) -> Union[str, CloneResult]:
        return await self.implementation(profile).clone(profile, name, url, text)

    async def design(self, profile: Profile, name: str) -> CloneResult:
        return await self.implementation(profile).design(profile, name)

    async def delete_voice(self, profile: Profile, voice_id: str) -> None:
        await self.implementation(profile).delete_voice(profile, voice_id)
