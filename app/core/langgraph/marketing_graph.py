"""TikTok Marketing Agent — advanced LangGraph workflow for TikTok Ads management.

Graph topology:

    [consensus?] → [strategist] → [chat] → (tool calls?) → [tool_call] → [chat] → ...
                                                                         → END

Consensus node (optional) runs when user mesajı yüksek belirsizlik içeriyorsa:
    - Aynı soruyu birden fazla modele sorar
    - Küçük bir hakem prompt'u ile ortak öneri/ülke listesi çıkarır
    - Bu notu sisteme ekler, final yanıtta belirtir

Strategist node tek sefer plan yazar; chat node plan + consensus notuyla çalışır.
"""

import asyncio
from typing import Optional

from langchain_core.messages import SystemMessage
from langfuse.langchain import CallbackHandler
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, StateGraph
from langgraph.graph.state import Command, CompiledStateGraph
from langgraph.types import RunnableConfig

from app.core.config import Environment, settings
from app.core.langgraph.graph import LangGraphAgent
from app.core.langgraph.tools.tiktok_ads import tiktok_ads_tools
from app.core.logging import logger
from app.core.metrics import llm_inference_duration_seconds
from app.core.prompts import load_marketing_prompt
from app.schemas.graph import MarketingGraphState
from app.services.llm import LLMRegistry, LLMService
from app.services.llm_models import DEFAULT_CONSENSUS_MODEL_NAMES, resolve_model_name
from app.utils import dump_messages, prepare_messages, process_llm_response


class TikTokMarketingAgent(LangGraphAgent):
    """Advanced LangGraph agent for TikTok Ads management with a strategist pre-planning node."""

    def __init__(self):
        self.llm_service = LLMService()
        self.llm_service.bind_tools(tiktok_ads_tools)
        self.tools_by_name = {t.name: t for t in tiktok_ads_tools}
        # Separate unbound LLM for the strategist (no tools needed)
        self._strategist_llm = LLMService()
        # Models used for consensus voting (configurable via env CONSENSUS_MODELS)
        try:
            self._consensus_models = [resolve_model_name(m) for m in settings.CONSENSUS_MODELS]
        except ValueError as e:
            logger.error("consensus_models_invalid", error=str(e))
            self._consensus_models = DEFAULT_CONSENSUS_MODEL_NAMES
        self._connection_pool = None
        self._graph = None
        self.memory = None
        logger.info(
            "tiktok_marketing_agent_initialized",
            model=settings.DEFAULT_LLM_MODEL,
            tools=[t.name for t in tiktok_ads_tools],
            environment=settings.ENVIRONMENT.value,
        )

    # ── Helper: detect high uncertainty —————————————————
    def _is_high_uncertainty(self, user_text: str) -> bool:
        # Heuristic: intent includes market/region selection OR short open question with '?'
        text = user_text.lower()
        keywords = ["hangi ülke", "hangi ulke", "hangi pazar", "nerede açalım", "hangi market", "region"]
        return any(k in text for k in keywords) or ("?" in text and len(text.split()) <= 20)

    async def _run_consensus(self, last_user_msg: dict) -> tuple[bool, str]:
        """Ask multiple models and synthesize a short consensus note."""
        async def call_model(model_name: str):
            llm = LLMRegistry.get(model_name)
            return await llm.ainvoke(
                [
                    {"role": "system", "content": "Answer briefly: list top 3 candidate countries/regions with 1-line reason each."},
                    {"role": "user", "content": last_user_msg.get("content", "")},
                ]
            )

        try:
            results = await asyncio.gather(*(call_model(m) for m in self._consensus_models))
            raw_answers = [r.content if hasattr(r, "content") else str(r) for r in results]

            referee_prompt = """
You are a referee. You are given multiple expert answers. Produce ONE merged recommendation:
- Pick the overlapping top 3 countries/regions if possible; otherwise choose the most justified.
- Give 1-line reason per country.
- Mention that consensus voting was used across models: {models}.
""".strip().format(models=", ".join(self._consensus_models))

            referee_llm = LLMRegistry.get(self._consensus_models[0])
            referee_resp = await referee_llm.ainvoke(
                [
                    {"role": "system", "content": referee_prompt},
                    {"role": "user", "content": "\n\n".join(raw_answers)},
                ]
            )
            consensus_note = referee_resp.content if hasattr(referee_resp, "content") else str(referee_resp)
            return True, consensus_note
        except Exception as e:
            logger.error("consensus_failed", error=str(e))
            return False, ""

    # ── Strategist node ——————————————————————————————

    async def _strategist(self, state: MarketingGraphState, config: RunnableConfig) -> Command:
        """Pre-planning node — produces a concise tool-use strategy before the chat node runs."""
        tool_names = ", ".join(self.tools_by_name.keys())
        strategist_system = (
            "You are an internal marketing strategy planner. "
            "Read the user's last message and decide the optimal sequence of tool calls needed to answer it. "
            "Reply with a SHORT bullet-point plan (3-5 lines max). "
            "Do NOT answer the user — only produce the plan. "
            f"Available tools: {tool_names}"
        )
        # Only pass the last user message to the strategist to keep it cheap
        last_user_msg = next(
            (m for m in reversed(dump_messages(state.messages)) if m.get("role") == "user"),
            None,
        )
        if not last_user_msg:
            return Command(update={"strategy": "No user message found."}, goto="chat")

        consensus_used = False
        consensus_note = ""
        if self._is_high_uncertainty(last_user_msg.get("content", "")):
            consensus_used, consensus_note = await self._run_consensus(last_user_msg)

        msgs = [SystemMessage(content=strategist_system), {"role": "user", "content": last_user_msg["content"]}]
        try:
            strategy_response = await self._strategist_llm.call(msgs)
            strategy = strategy_response.content if hasattr(strategy_response, "content") else str(strategy_response)
            logger.info(
                "marketing_strategy_generated",
                session_id=config["configurable"]["thread_id"],
                strategy_preview=strategy[:120],
            )
            return Command(
                update={
                    "strategy": strategy,
                    "consensus_used": consensus_used,
                    "consensus_note": consensus_note,
                },
                goto="chat",
            )
        except Exception as e:
            logger.error("marketing_strategist_failed", error=str(e))
            return Command(update={"strategy": "", "consensus_used": consensus_used, "consensus_note": consensus_note}, goto="chat")

    # ── Chat node —————————————————————————————————

    async def _chat(self, state: MarketingGraphState, config: RunnableConfig) -> Command:
        """Main chat node — uses the marketing prompt + strategy context to call tools or respond."""
        current_llm = self.llm_service.get_llm()
        model_name = (
            current_llm.model_name
            if current_llm and hasattr(current_llm, "model_name")
            else settings.DEFAULT_LLM_MODEL
        )

        strategy_context = state.strategy or ""
        consensus_note = state.consensus_note or ""
        system_prompt = load_marketing_prompt(
            long_term_memory=state.long_term_memory,
            strategy_context=f"\n\n# Current Turn Strategy\n{strategy_context}" if strategy_context else "",
            consensus_note=f"\n\n# Consensus\n{consensus_note}\n\n(Consensus voting used across models.)" if state.consensus_used else "",
        )
        messages = prepare_messages(state.messages, current_llm, system_prompt)

        try:
            with llm_inference_duration_seconds.labels(model=model_name).time():
                response_message = await self.llm_service.call(dump_messages(messages))

            response_message = process_llm_response(response_message)

            logger.info(
                "marketing_llm_response_generated",
                session_id=config["configurable"]["thread_id"],
                model=model_name,
                used_strategy=bool(strategy_context),
                has_tool_calls=bool(response_message.tool_calls),
            )

            goto = "tool_call" if response_message.tool_calls else END
            return Command(update={"messages": [response_message]}, goto=goto)
        except Exception as e:
            logger.error(
                "marketing_llm_call_failed",
                session_id=config["configurable"]["thread_id"],
                error=str(e),
            )
            raise Exception(f"marketing agent failed: {str(e)}")

    # ── Graph factory ——————————————————————————————

    async def create_graph(self) -> Optional[CompiledStateGraph]:
        """Build the marketing graph: strategist → chat → tool_call → chat → ..."""
        if self._graph is None:
            try:
                graph_builder = StateGraph(MarketingGraphState)
                graph_builder.add_node("strategist", self._strategist, ends=["chat"])
                graph_builder.add_node("chat", self._chat, ends=["tool_call", END])
                graph_builder.add_node("tool_call", self._tool_call, ends=["chat"])
                graph_builder.set_entry_point("strategist")
                graph_builder.set_finish_point("chat")

                connection_pool = await self._get_connection_pool()
                if connection_pool:
                    checkpointer = AsyncPostgresSaver(connection_pool)
                    await checkpointer.setup()
                else:
                    checkpointer = None
                    if settings.ENVIRONMENT != Environment.PRODUCTION:
                        raise Exception("Connection pool initialization failed")

                self._graph = graph_builder.compile(
                    checkpointer=checkpointer,
                    name=f"{settings.PROJECT_NAME} Marketing Agent ({settings.ENVIRONMENT.value})",
                )
                logger.info(
                    "marketing_graph_created",
                    environment=settings.ENVIRONMENT.value,
                    has_checkpointer=checkpointer is not None,
                )
            except Exception as e:
                logger.error("marketing_graph_creation_failed", error=str(e))
                if settings.ENVIRONMENT == Environment.PRODUCTION:
                    return None
                raise e
        return self._graph

