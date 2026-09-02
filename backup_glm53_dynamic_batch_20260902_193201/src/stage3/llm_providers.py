from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ModelOption:
    id: str
    label: str
    free: bool = False


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    protocol: str
    key_env: str
    base_url: str
    models: tuple[ModelOption, ...]
    thinking_style: str = "none"
    json_mode: bool = True
    temperature: bool = True
    custom_model: bool = False
    custom_base_url: bool = False
    default_thinking: str = "disabled"

    @property
    def default_model(self) -> str:
        return self.models[0].id


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "deepseek",
        "DeepSeek",
        "openai",
        "DEEPSEEK_API_KEY",
        "https://api.deepseek.com",
        (
            ModelOption("deepseek-v4-flash", "DeepSeek V4 Flash"),
            ModelOption("deepseek-v4-pro", "DeepSeek V4 Pro"),
        ),
        thinking_style="object",
    ),
    ProviderSpec(
        "zhipu",
        "智谱 GLM",
        "openai",
        "ZHIPU_API_KEY",
        "https://open.bigmodel.cn/api/paas/v4/",
        (
        ModelOption("glm-5.3-flash", "GLM-5.3-Flash"),
        ModelOption("glm-4.7-flash", "GLM-4.7-Flash（免费）", free=True),
        ModelOption("glm-4.7-flashx", "GLM-4.7-FlashX"),
        ModelOption("glm-5.2", "GLM-5.2"),
        ),
        thinking_style="object",
    ),
    ProviderSpec(
        "qwen",
        "阿里云百炼 / 通义千问",
        "openai",
        "DASHSCOPE_API_KEY",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        (
            ModelOption("qwen3.7-flash", "Qwen3.7-Flash"),
            ModelOption("qwen3.7-plus", "Qwen3.7-Plus"),
            ModelOption("qwen-mt-plus", "Qwen-MT-Plus（翻译）"),
        ),
        thinking_style="boolean",
    ),
    ProviderSpec(
        "kimi",
        "Moonshot / Kimi",
        "openai",
        "MOONSHOT_API_KEY",
        "https://api.moonshot.cn/v1",
        (
            ModelOption("kimi-k2.6", "Kimi K2.6"),
            ModelOption("kimi-k2.5", "Kimi K2.5"),
        ),
        thinking_style="object",
        temperature=False,
    ),
    ProviderSpec(
        "minimax",
        "MiniMax",
        "openai",
        "MINIMAX_API_KEY",
        "https://api.minimaxi.com/v1",
        (
            ModelOption("MiniMax-M2.7", "MiniMax M2.7"),
            ModelOption("MiniMax-M2.7-highspeed", "MiniMax M2.7 Highspeed"),
        ),
        thinking_style="minimax",
        json_mode=False,
        temperature=False,
    ),
    ProviderSpec(
        "doubao",
        "火山方舟 / 豆包",
        "openai",
        "ARK_API_KEY",
        "https://ark.cn-beijing.volces.com/api/v3",
        (ModelOption("doubao-seed-2-0-lite-260215", "Doubao Seed 2.0 Lite"),),
        thinking_style="object",
    ),
    ProviderSpec(
        "openai",
        "OpenAI",
        "openai",
        "OPENAI_API_KEY",
        "https://api.openai.com/v1",
        (
            ModelOption("gpt-5.6-luna", "GPT-5.6 Luna"),
            ModelOption("gpt-5.6-terra", "GPT-5.6 Terra"),
            ModelOption("gpt-5.6-sol", "GPT-5.6 Sol"),
            ModelOption("gpt-4.1-mini", "GPT-4.1 mini"),
        ),
        thinking_style="openai",
        temperature=False,
    ),
    ProviderSpec(
        "anthropic",
        "Anthropic / Claude",
        "anthropic",
        "ANTHROPIC_API_KEY",
        "https://api.anthropic.com",
        (
            ModelOption("claude-haiku-4-5", "Claude Haiku 4.5"),
            ModelOption("claude-sonnet-5", "Claude Sonnet 5"),
            ModelOption("claude-opus-5", "Claude Opus 5"),
        ),
        thinking_style="anthropic",
        json_mode=False,
        temperature=False,
    ),
    ProviderSpec(
        "custom",
        "自定义 OpenAI 兼容接口",
        "openai",
        "CUSTOM_LLM_API_KEY",
        "http://127.0.0.1:8000/v1",
        (ModelOption("custom-model", "自定义模型"),),
        thinking_style="object",
        json_mode=False,
        custom_model=True,
        custom_base_url=True,
    ),
)

PROVIDER_BY_ID = {provider.id: provider for provider in PROVIDERS}
API_KEY_ENV_NAMES = tuple(provider.key_env for provider in PROVIDERS)


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def load_llm_settings() -> dict[str, Any]:
    """Load the active translation provider without exposing secrets to callers that do not ask for it."""
    _load_dotenv()
    provider_id = os.environ.get("TRANSLATION_PROVIDER", "deepseek").strip().casefold()
    provider = PROVIDER_BY_ID.get(provider_id, PROVIDER_BY_ID["deepseek"])
    legacy_model = os.environ.get("DEEPSEEK_MODEL", "") if provider.id == "deepseek" else ""
    legacy_base_url = os.environ.get("DEEPSEEK_BASE_URL", "") if provider.id == "deepseek" else ""
    model = os.environ.get("TRANSLATION_MODEL", "").strip() or legacy_model or provider.default_model
    base_url = os.environ.get("TRANSLATION_BASE_URL", "").strip() or legacy_base_url or provider.base_url
    thinking = os.environ.get(
        "TRANSLATION_THINKING", provider.default_thinking
    ).strip().casefold()
    if thinking not in {"disabled", "enabled"}:
        thinking = "disabled"
    is_glm53_flash = (
    provider.id == "zhipu"
    and model.casefold() == "glm-5.3-flash"
    )
    if is_glm53_flash:
        thinking = "enabled"
    reasoning_effort = os.environ.get(
        "TRANSLATION_REASONING_EFFORT",
        "low" if is_glm53_flash else "",
    ).strip().casefold()
    if is_glm53_flash and reasoning_effort not in {"low", "high", "max"}:
        reasoning_effort = "low"
    return {
        "provider": provider.id,
        "provider_label": provider.label,
        "protocol": provider.protocol,
        "key_env": provider.key_env,
        "api_key": os.environ.get(provider.key_env, "").strip(),
        "base_url": base_url,
        "model": model,
        "thinking": thinking,
        "batch_size": _bounded_int("TRANSLATION_BATCH_SIZE", 32, 1, 100),
        "context_before": _bounded_int("TRANSLATION_CONTEXT_BEFORE", 2, 0, 10),
        "context_after": _bounded_int("TRANSLATION_CONTEXT_AFTER", 2, 0, 10),
        "max_output_tokens": _bounded_int("TRANSLATION_MAX_OUTPUT_TOKENS", 4096, 256, 32768),
        "reasoning_effort": reasoning_effort,
    }


def public_provider_catalog(
    configured_keys: set[str] | None = None,
    values: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    _load_dotenv()
    source = values if values is not None else os.environ
    configured = configured_keys if configured_keys is not None else {
        name for name in API_KEY_ENV_NAMES if str(source.get(name, "")).strip()
    }
    if values is None:
        active = load_llm_settings()
    else:
        provider = PROVIDER_BY_ID.get(
            str(source.get("TRANSLATION_PROVIDER", "deepseek")).strip().casefold(),
            PROVIDER_BY_ID["deepseek"],
        )
        legacy_model = str(source.get("DEEPSEEK_MODEL", "")) if provider.id == "deepseek" else ""
        legacy_url = str(source.get("DEEPSEEK_BASE_URL", "")) if provider.id == "deepseek" else ""

        def value_int(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                number = int(source.get(name, default))
            except (TypeError, ValueError):
                number = default
            return min(maximum, max(minimum, number))

        thinking = str(
            source.get("TRANSLATION_THINKING", provider.default_thinking)
        ).casefold()
        active = {
            "provider": provider.id,
            "model": str(source.get("TRANSLATION_MODEL", "")).strip() or legacy_model or provider.default_model,
            "base_url": str(source.get("TRANSLATION_BASE_URL", "")).strip() or legacy_url or provider.base_url,
            "thinking": thinking if thinking in {"enabled", "disabled"} else "disabled",
            "batch_size": value_int("TRANSLATION_BATCH_SIZE", 32, 1, 100),
            "context_before": value_int("TRANSLATION_CONTEXT_BEFORE", 2, 0, 10),
            "context_after": value_int("TRANSLATION_CONTEXT_AFTER", 2, 0, 10),
            "max_output_tokens": value_int("TRANSLATION_MAX_OUTPUT_TOKENS", 4096, 256, 32768),
        }
    providers = []
    for provider in PROVIDERS:
        providers.append(
            {
                "id": provider.id,
                "label": provider.label,
                "key_env": provider.key_env,
                "configured": provider.key_env in configured,
                "base_url": provider.base_url,
                "custom_base_url": provider.custom_base_url,
                "custom_model": provider.custom_model,
                "thinking": provider.thinking_style != "none",
                "default_thinking": provider.default_thinking,
                "models": [
                    {"id": model.id, "label": model.label, "free": model.free}
                    for model in provider.models
                ],
            }
        )
    return {
        "active": {
            key: active[key]
            for key in (
                "provider",
                "model",
                "base_url",
                "thinking",
                "batch_size",
                "context_before",
                "context_after",
                "max_output_tokens",
            )
        },
        "providers": providers,
    }


__all__ = [
    "API_KEY_ENV_NAMES",
    "PROVIDERS",
    "PROVIDER_BY_ID",
    "ModelOption",
    "ProviderSpec",
    "load_llm_settings",
    "public_provider_catalog",
]
