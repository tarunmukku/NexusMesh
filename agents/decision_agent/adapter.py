"""DecisionLangGraphAdapter implementation.

A custom subclass of LangGraphAdapter that overrides on_message to detect if a thread is interrupted
"""

from __future__ import annotations

import json
import logging
from typing import Any, List

from langgraph.types import Command
from thenvoi.adapters import LangGraphAdapter
from thenvoi.core.types import Capability, PlatformMessage
from thenvoi.core.protocols import AgentToolsProtocol
from thenvoi.converters.langchain import LangChainMessages
from thenvoi.integrations.langgraph.langchain_tools import agent_tools_to_langchain
from band.SafeSendLangGraphAdapter import (
    SafeSendLangGraphAdapter,
    WrappedAgentTools,
    _extract_mention_handles,
    _PARTICIPANTS,
)

logger = logging.getLogger("nexusmesh.decision.adapter")
_fh_adapter = logging.FileHandler("decision_debug.log")
_fh_adapter.setLevel(logging.DEBUG)
_fh_adapter.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
)
logger.addHandler(_fh_adapter)
logger.setLevel(logging.DEBUG)


class DecisionLangGraphAdapter(SafeSendLangGraphAdapter):
    """LangGraph adapter customized for human-in-the-loop interrupts and resumption."""

    async def on_message(
        self,
        msg: PlatformMessage,
        tools: AgentToolsProtocol,
        history: LangChainMessages,
        participants_msg: str | None,
        contacts_msg: str | None,
        *,
        is_session_bootstrap: bool,
        room_id: str,
    ) -> None:
        logger.info(
            "[HANDLE] DecisionAgent on_message: msg %s in room %s", msg.id, room_id
        )

        self._captured_ai_content = ""
        fallback_mentions = self._resolve_fallback_mentions(msg)

        wrapped_tools = WrappedAgentTools(tools, fallback_mentions=fallback_mentions)
        langchain_tools = (
            agent_tools_to_langchain(
                wrapped_tools,
                include_memory_tools=Capability.MEMORY in self.features.capabilities,
                include_contacts=Capability.CONTACTS in self.features.capabilities,
            )
            + self.additional_tools
        )

        if self.graph_factory:
            graph = self.graph_factory(langchain_tools)
        else:
            graph = self._static_graph

        if not graph:
            raise RuntimeError("No graph available")

        thread_config = {"configurable": {"thread_id": room_id}}
        try:
            state = await graph.aget_state(thread_config)
            is_interrupted = False
            for task in state.tasks:
                if task.interrupts:
                    is_interrupted = True
                    logger.info(f"Found pending interrupt: {task.interrupts}")
                    break
        except Exception as e:
            logger.warning(f"Failed to check graph state: {e}")
            is_interrupted = False

        if is_interrupted:
            logger.info(
                "DecisionAgent is in an interrupted state. Resuming via Command."
            )
            graph_input = Command(resume=msg.format_for_llm())
        else:
            logger.info("DecisionAgent is in normal state. Loading message history.")
            messages: List[Any] = []
            if is_session_bootstrap:
                if self.graph_factory and room_id not in self._bootstrapped_rooms:
                    messages.append(("system", self._system_prompt))
                    self._bootstrapped_rooms.add(room_id)
                if history:
                    messages.extend(history)
            if participants_msg:
                messages.append(("user", f"[System]: {participants_msg}"))
            if contacts_msg:
                messages.append(("user", f"[System]: {contacts_msg}"))

            messages.append(("user", msg.format_for_llm()))
            graph_input = {"messages": messages}

        try:
            async for event in graph.astream_events(
                graph_input,
                config={
                    "configurable": {"thread_id": room_id},
                    "recursion_limit": self.recursion_limit,
                },
                version="v2",
            ):
                await self._handle_stream_event(event, room_id, wrapped_tools)

            logger.info("[DONE] Message %s processed successfully", msg.id)

            final_content = (
                wrapped_tools.explicit_content
                if wrapped_tools.explicit_content
                else self._captured_ai_content
            )

            state = graph.get_state(config={"configurable": {"thread_id": room_id}})
            if not final_content and state.next:
                for task in state.tasks:
                    if task.interrupts:
                        inter = task.interrupts[0].value
                        if (
                            isinstance(inter, dict)
                            and inter.get("message_type") == "hitl_escalation"
                        ):
                            escs = inter.get("escalations", [])
                            final_content = f"⚠️ **Action Required**: {len(escs)} RED claims require your review. Please reply with your Approve/Decline decisions."
                            break

            if final_content:
                if wrapped_tools.explicit_mentions:
                    mentions = wrapped_tools.explicit_mentions
                else:
                    mentions = _extract_mention_handles(final_content, _PARTICIPANTS)
                    if not mentions:
                        mentions = fallback_mentions

                if not mentions:
                    mentions = ["tarun.mukku"]

                logger.info(
                    "DecisionAgent auto-sending message directly via code (%d chars)",
                    len(final_content),
                )
                await tools.send_message(content=final_content, mentions=mentions)
            else:
                logger.warning("DecisionAgent generated no content to send.")

        except Exception as e:
            logger.error("Error processing message %s: %s", msg.id, e, exc_info=True)
            try:
                await tools.send_event(content=f"Error: {e}", message_type="error")
            except Exception:
                pass
            raise
