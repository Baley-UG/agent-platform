"""Centralized LLM model definitions.

Keeps provider, model id, and provider-qualified name in one place.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List


@dataclass(frozen=True)
class LLMModel:
    """Immutable LLM model metadata."""

    name: str
    provider: str
    model: str
    provider_model: str  # provider/model
    tier: "ModelTier"
    cost_per_1k_input: float | None
    cost_per_1k_output: float | None
    max_output_tokens: int | None


class ModelTier(str, Enum):
    QUALITY = "quality"
    BALANCED = "balanced"
    FAST = "fast"


class LLMModels:
    GPT_4O_MINI = LLMModel(
        name="gpt-4o-mini",
        provider="openai",
        model="gpt-4o-mini",
        provider_model="openai/gpt-4o-mini",
        tier=ModelTier.FAST,
        cost_per_1k_input=None,
        cost_per_1k_output=None,
        max_output_tokens=4096,
    )

    GPT_4O = LLMModel(
        name="gpt-4o",
        provider="azure        docker compose --env-file .env logs -f app",
        model="gpt-4o",
        provider_model="openai/gpt-4o",
        tier=ModelTier.QUALITY,
        cost_per_1k_input=None,
        cost_per_1k_output=None,
        max_output_tokens=8192,
    )

    CLAUDE_3_5_SONNET = LLMModel(
        name="claude-3-5-sonnet",
        provider="anthropic",
        model="claude-3.5-sonnet",
        provider_model="anthropic/claude-3.5-sonnet",
        tier=ModelTier.QUALITY,
        cost_per_1k_input=None,
        cost_per_1k_output=None,
        max_output_tokens=8192,
    )

    GEMINI_2_FLASH = LLMModel(
        name="gemini-2-flash",
        provider="google",
        model="gemini-2.0-flash-001",
        provider_model="google/gemini-2.0-flash-001",
        tier=ModelTier.FAST,
        cost_per_1k_input=None,
        cost_per_1k_output=None,
        max_output_tokens=4096,
    )

    DEEPSEEK_R1 = LLMModel(
        name="deepseek-r1",
        provider="deepseek",
        model="deepseek-r1",
        provider_model="deepseek/deepseek-r1",
        tier=ModelTier.BALANCED,
        cost_per_1k_input=None,
        cost_per_1k_output=None,
        max_output_tokens=8192,
    )


LLM_MODELS: List[LLMModel] = [
    LLMModels.GPT_4O_MINI,
    LLMModels.GPT_4O,
    LLMModels.CLAUDE_3_5_SONNET,
    LLMModels.GEMINI_2_FLASH,
    LLMModels.DEEPSEEK_R1,
]

LLM_MODEL_BY_NAME: Dict[str, LLMModel] = {model.name: model for model in LLM_MODELS}
PROVIDER_BY_NAME: Dict[str, str] = {model.name: model.provider_model for model in LLM_MODELS}
NAME_BY_PROVIDER: Dict[str, str] = {model.provider_model: model.name for model in LLM_MODELS}

# Default short names used for consensus voting (aligns with LLM_MODELS names)
DEFAULT_CONSENSUS_MODEL_NAMES = [
    LLMModels.GPT_4O.name,
    LLMModels.CLAUDE_3_5_SONNET.name,
    LLMModels.GEMINI_2_FLASH.name,
]


def resolve_model_name(value: str) -> str:
    """Normalize a model identifier to the registry name.

    Accepts either the short registry name or the provider-specific model string.
    Raises ValueError if the model is unknown.
    """

    value_str = str(value)

    if value_str in LLM_MODEL_BY_NAME:
        return value_str

    for model in LLM_MODELS:
        if model.provider_model == value_str or model.model == value_str:
            return model.name

    raise ValueError(f"unknown model '{value_str}'")


def get_provider_model(value: str) -> str:
    """Return provider-specific model id for a given registry name or raise ValueError."""

    value_str = resolve_model_name(value)
    provider = PROVIDER_BY_NAME.get(value_str)
    if not provider:
        raise ValueError(f"provider not found for model '{value_str}'")
    return provider
