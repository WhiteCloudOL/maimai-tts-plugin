"""服务商注册表；新增平台无需修改调度器。"""

from .dashscope import CosyVoiceProvider
from .compatible import CompatibleProvider
from .qwen import QwenProvider
from .gpt_sovits import GPTSoVITSProvider
from .mimo import MiMoProvider
from .bailian_minimax import BailianMiniMaxProvider
from .minimax import MiniMaxProvider
from .openai import OpenAIProvider
from .siliconflow import SiliconFlowProvider
from .volcengine import VolcengineProvider
from .breeze import BreezeProvider


PROVIDERS = {
    "breeze": BreezeProvider,
    "openai": OpenAIProvider,
    "compatible": CompatibleProvider,
    "qwen": QwenProvider,
    "cosyvoice": CosyVoiceProvider,
    "bailian_minimax": BailianMiniMaxProvider,
    "minimax": MiniMaxProvider,
    "siliconflow": SiliconFlowProvider,
    "mimo": MiMoProvider,
    "volcengine": VolcengineProvider,
    "gpt_sovits": GPTSoVITSProvider,
}
