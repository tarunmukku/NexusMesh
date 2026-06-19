"""SafeSendLangGraphAdapter — wraps AgentToolsProtocol and auto-sends unsent AI text."""

import logging
import re
from typing import Any

from thenvoi.adapters import LangGraphAdapter
from thenvoi.core.protocols import AgentToolsProtocol

logger = logging.getLogger("nexusmesh.safe_send")

_MENTION_RE = re.compile(r"@\[\[([0-9a-f-]{36})\]\]")


def _extract_mention_handles(content: str, participants: dict[str, str]) -> list[str]:
    """Extract participant handles from @[[uuid]] patterns in content.

    Args:
        content: message text potentially containing @[[uuid]] patterns.
        participants: mapping of agent_id -> handle (e.g. "@tarun.mukku/decision-agent").

    Returns:
        List of handle strings recognised by the platform, de-duplicated.
    """
    ids = _MENTION_RE.findall(content)
    handles = []
    seen = set()
    for uid in ids:
        handle = participants.get(uid)
        if handle and handle not in seen:
            handles.append(handle)
            seen.add(handle)
    return handles


def _load_participants() -> dict[str, str]:
    """Load agent_id -> handle map from agent_config.yaml."""
    try:
        import yaml
        from pathlib import Path

        for p in [
            Path("agent_config.yaml"),
            Path(__file__).resolve().parent.parent / "agent_config.yaml",
        ]:
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f)
                    return {
                        v["agent_id"]: v["handler"]
                        for v in cfg.values()
                        if isinstance(v, dict)
                        and v.get("agent_id")
                        and v.get("handler")
                    }
    except Exception:  # noqa: BLE001
        pass
    return {}


_PARTICIPANTS = _load_participants()


class WrappedAgentTools:
    """Wrapper for AgentToolsProtocol that intercepts send_message to make it a no-op
    for the LLM, returning a success string. We will handle the actual sending in the
    adapter using the final captured text.
    """

    def __init__(
        self, tools: AgentToolsProtocol, fallback_mentions: list[str] | None = None
    ):
        self._tools = tools
        self.message_sent = False
        self._fallback_mentions = fallback_mentions or []
        self.explicit_content = ""
        self.explicit_mentions = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tools, name)

    async def send_message(
        self, content: str = "", mentions: list[str] | None = None, **kwargs
    ) -> str:
        self.explicit_content = content
        self.explicit_mentions = mentions or []
        self.message_sent = True
        return "Message sent successfully."


class SafeSendLangGraphAdapter(LangGraphAdapter):
    """LangGraphAdapter that prevents message loops by wrapping AgentToolsProtocol."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._captured_ai_content: str = ""
        self._fallback_mentions: list[str] = []

    async def _handle_stream_event(self, event, room_id, tools):
        """Override to capture the last AI text output that has no tool calls."""
        evt_type = event.get("event", "")
        if evt_type == "on_chat_model_end":
            output = event.get("data", {}).get("output")
            if output is not None:
                content = getattr(output, "content", None)
                tool_calls = getattr(output, "tool_calls", None)
                if (
                    content
                    and isinstance(content, str)
                    and content.strip()
                    and not tool_calls
                ):
                    self._captured_ai_content = content.strip()
        await super()._handle_stream_event(event, room_id, tools)

    def _resolve_fallback_mentions(self, msg) -> list[str]:
        """Determine fallback mentions from the incoming message sender."""
        sender_id = getattr(msg, "sender_id", None)
        if sender_id:
            handle = _PARTICIPANTS.get(sender_id)
            if handle:
                return [handle]
        for handle in _PARTICIPANTS.values():
            if "/" not in handle.lstrip("@"):
                return [handle]
        return []

    async def on_message(self, msg, tools, *args, **kwargs) -> None:
        self._captured_ai_content = ""
        self._fallback_mentions = self._resolve_fallback_mentions(msg)
        wrapped_tools = WrappedAgentTools(
            tools, fallback_mentions=self._fallback_mentions
        )
        await super().on_message(msg, wrapped_tools, *args, **kwargs)
        final_content = (
            wrapped_tools.explicit_content
            if wrapped_tools.explicit_content
            else self._captured_ai_content
        )

        if final_content:
            if wrapped_tools.explicit_mentions:
                mentions = wrapped_tools.explicit_mentions
            else:
                mentions = _extract_mention_handles(final_content, _PARTICIPANTS)
                if not mentions:
                    mentions = self._fallback_mentions
            if not mentions:
                mentions = ["tarun.mukku"]

            logger.info(
                "Auto-sending message directly via code (%d chars)", len(final_content)
            )
            await tools.send_message(content=final_content, mentions=mentions)
        else:
            logger.warning("No content generated by agent to send.")
