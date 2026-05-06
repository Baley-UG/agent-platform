"""LLM service for managing LLM calls with retries and fallback mechanisms."""

from typing import (
    Any,
    Dict,
    List,
    Optional,
)

from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI
from openai import (
    APIError,
    APITimeoutError,
    OpenAIError,
    RateLimitError,
)
def _log_retry(retry_state) -> None:
    """Structured log for tenacity retries with snake_case event."""

    exc = retry_state.outcome.exception()
    logger.warning(
        "llm_call_retrying",
        attempt=retry_state.attempt_number,
        sleep=retry_state.next_action.sleep if retry_state.next_action else None,
        error=str(exc) if exc else None,
    )

from app.core.config import (
    Environment,
    settings,
)
from app.core.logging import logger
from app.services.llm_models import (
    LLM_MODELS,
    LLMModel,
    resolve_model_name,
)


class LLMRegistry:
    """Registry of available LLM models with pre-initialized instances.

    This class maintains a list of LLM configurations and provides
    methods to retrieve them by name with optional argument overrides.
    """

    LLMS: List[Dict[str, Any]] = []

    @staticmethod
    def _base_llm_kwargs(model_meta: LLMModel) -> Dict[str, Any]:
        """Common kwargs per model, including per-model tuning."""

        kwargs: Dict[str, Any] = {
            "model": model_meta.provider_model,
            "api_key": settings.OPENROUTER_API_KEY,
            "base_url": settings.LLM_BASE_URL,
            "max_tokens": model_meta.max_output_tokens or settings.MAX_TOKENS,
            "temperature": settings.DEFAULT_LLM_TEMPERATURE,
        }

        if model_meta.name == "gpt-4o-mini":
            kwargs["top_p"] = 0.9 if settings.ENVIRONMENT == Environment.PRODUCTION else 0.8
        if model_meta.name == "gpt-4o":
            kwargs["top_p"] = 0.95 if settings.ENVIRONMENT == Environment.PRODUCTION else 0.8
            kwargs["presence_penalty"] = 0.1 if settings.ENVIRONMENT == Environment.PRODUCTION else 0.0
            kwargs["frequency_penalty"] = 0.1 if settings.ENVIRONMENT == Environment.PRODUCTION else 0.0

        # Override tokenizer model to avoid unsupported provider ids like openai/gpt-4o-mini
        kwargs["tiktoken_model_name"] = model_meta.name

        return kwargs

    @classmethod
    def _ensure_initialized(cls):
        """Lazily build the registry from the canonical model list."""

        if cls.LLMS:
            return

        cls.LLMS = []
        for model in LLM_MODELS:
            kwargs = cls._base_llm_kwargs(model)
            cls.LLMS.append({
                "name": model.name,
                "provider_model": model.provider_model,
                "meta": model,
                "llm": ChatOpenAI(**kwargs),
            })

    @classmethod
    def get(cls, model_name: str, **kwargs) -> BaseChatModel:
        """Get an LLM by name with optional argument overrides.

        Args:
            model_name: Name of the model to retrieve
            **kwargs: Optional arguments to override default model configuration

        Returns:
            BaseChatModel instance

        Raises:
            ValueError: If model_name is not found in LLMS
        """
        cls._ensure_initialized()

        try:
            resolved_name = resolve_model_name(model_name)
        except ValueError as e:
            logger.warning("unknown_model_resolution_failed", requested=model_name, error=str(e))
            raise

        # Find the model in the registry
        model_entry = None
        for entry in cls.LLMS:
            if entry["name"] == resolved_name:
                model_entry = entry
                break

        if not model_entry:
            available_models = [entry["name"] for entry in cls.LLMS]
            raise ValueError(
                f"model '{model_name}' not found in registry. available models: {', '.join(available_models)}"
            )

        # If user provides kwargs, create a new instance with those args
        if kwargs:
            merged_kwargs = {**cls._base_llm_kwargs(model_entry["meta"]), **kwargs}
            logger.debug("creating_llm_with_custom_args", model_name=model_entry["name"], custom_args=list(kwargs.keys()))
            return ChatOpenAI(**merged_kwargs)

        # Return the default instance
        logger.debug("using_default_llm_instance", model_name=model_entry["name"])
        return model_entry["llm"]

    @classmethod
    def get_all_names(cls) -> List[str]:
        """Get all registered LLM names in order.

        Returns:
            List of LLM names
        """
        cls._ensure_initialized()
        return [entry["name"] for entry in cls.LLMS]

    @classmethod
    def get_model_at_index(cls, index: int) -> Dict[str, Any]:
        """Get model entry at specific index.

        Args:
            index: Index of the model in LLMS list

        Returns:
            Model entry dict
        """
        cls._ensure_initialized()
        if 0 <= index < len(cls.LLMS):
            return cls.LLMS[index]
        return cls.LLMS[0]  # Wrap around to first model


class LLMService:
    """Service for managing LLM calls with retries and circular fallback.

    This service handles all LLM interactions with automatic retry logic,
    rate limit handling, and circular fallback through all available models.
    """

    def __init__(self):
        """Initialize the LLM service."""
        self._llm: Optional[BaseChatModel] = None
        self._current_model_index: int = 0

        # Find index of default model in registry
        all_names = LLMRegistry.get_all_names()
        try:
            self._current_model_index = all_names.index(settings.DEFAULT_LLM_MODEL)
            self._llm = LLMRegistry.get(settings.DEFAULT_LLM_MODEL)
            logger.info(
                "llm_service_initialized",
                default_model=settings.DEFAULT_LLM_MODEL,
                model_index=self._current_model_index,
                total_models=len(all_names),
                environment=settings.ENVIRONMENT.value,
            )
        except (ValueError, Exception) as e:
            # Default model not found, use first model
            self._current_model_index = 0
            self._llm = LLMRegistry.LLMS[0]["llm"]
            logger.warning(
                "default_model_not_found_using_first",
                requested=settings.DEFAULT_LLM_MODEL,
                using=all_names[0] if all_names else "none",
                error=str(e),
            )

    def _get_next_model_index(self) -> int:
        """Get the next model index in circular fashion.

        Returns:
            Next model index (wraps around to 0 if at end)
        """
        LLMRegistry._ensure_initialized()
        total_models = len(LLMRegistry.LLMS)
        next_index = (self._current_model_index + 1) % total_models
        return next_index

    def _switch_to_next_model(self) -> bool:
        """Switch to the next model in the registry (circular).

        Returns:
            True if successfully switched, False otherwise
        """
        LLMRegistry._ensure_initialized()
        try:
            next_index = self._get_next_model_index()
            next_model_entry = LLMRegistry.get_model_at_index(next_index)

            logger.warning(
                "switching_to_next_model",
                from_index=self._current_model_index,
                to_index=next_index,
                to_model=next_model_entry["name"],
            )

            self._current_model_index = next_index
            self._llm = next_model_entry["llm"]

            logger.info("model_switched", new_model=next_model_entry["name"], new_index=next_index)
            return True
        except Exception as e:
            logger.error("model_switch_failed", error=str(e))
            return False

    @retry(
        stop=stop_after_attempt(settings.MAX_LLM_CALL_RETRIES),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((RateLimitError, APITimeoutError, APIError)),
        before_sleep=_log_retry,
        reraise=True,
    )
    async def _call_llm_with_retry(self, messages: List[BaseMessage]) -> BaseMessage:
        """Call the LLM with automatic retry logic.

        Args:
            messages: List of messages to send to the LLM

        Returns:
            BaseMessage response from the LLM

        Raises:
            OpenAIError: If all retries fail
        """
        if not self._llm:
            raise RuntimeError("llm not initialized")

        try:
            response = await self._llm.ainvoke(messages)
            logger.debug("llm_call_successful", message_count=len(messages))
            return response
        except (RateLimitError, APITimeoutError, APIError) as e:
            logger.warning(
                "llm_call_failed_retrying",
                error_type=type(e).__name__,
                error=str(e),
                exc_info=True,
            )
            raise
        except OpenAIError as e:
            logger.error(
                "llm_call_failed",
                error_type=type(e).__name__,
                error=str(e),
            )
            raise

    async def call(
        self,
        messages: List[BaseMessage],
        model_name: Optional[str] = None,
        **model_kwargs,
    ) -> BaseMessage:
        """Call the LLM with the specified messages and circular fallback.

        Args:
            messages: List of messages to send to the LLM
            model_name: Optional specific model to use. If None, uses current model.
            **model_kwargs: Optional kwargs to override default model configuration

        Returns:
            BaseMessage response from the LLM

        Raises:
            RuntimeError: If all models fail after retries
        """
        # If user specifies a model, get it from registry
        if model_name:
            try:
                resolved_model_name = resolve_model_name(model_name)
                self._llm = LLMRegistry.get(resolved_model_name, **model_kwargs)
                # Update index to match the requested model
                all_names = LLMRegistry.get_all_names()
                try:
                    self._current_model_index = all_names.index(resolved_model_name)
                except ValueError:
                    pass  # Keep current index if model name not in list
                logger.info(
                    "using_requested_model",
                    model_name=resolved_model_name,
                    has_custom_kwargs=bool(model_kwargs),
                )
            except ValueError as e:
                logger.error("requested_model_not_found", model_name=model_name, error=str(e))
                raise

        # Call with retry; no circular fallback — use the configured model only
        try:
            return await self._call_llm_with_retry(messages)
        except OpenAIError as e:
            current_model_name = LLMRegistry.LLMS[self._current_model_index]["name"]
            logger.error(
                "llm_call_failed_after_retries",
                model=current_model_name,
                error=str(e),
            )
            raise RuntimeError(f"llm call failed after retries. last error: {str(e)}")

    def get_llm(self) -> Optional[BaseChatModel]:
        """Get the current LLM instance.

        Returns:
            Current BaseChatModel instance or None if not initialized
        """
        return self._llm

    def bind_tools(self, tools: List) -> "LLMService":
        """Bind tools to the current LLM.

        Args:
            tools: List of tools to bind

        Returns:
            Self for method chaining
        """
        if self._llm:
            self._llm = self._llm.bind_tools(tools)
            logger.debug("tools_bound_to_llm", tool_count=len(tools))
        return self


# Create global LLM service instance
llm_service = LLMService()
