"""Mention-gating preprocessor for NexusMesh Guard Band agents."""

from __future__ import annotations

import logging
import os
import re

from thenvoi.platform.event import MessageEvent, PlatformEvent
from thenvoi.preprocessing.default import DefaultPreprocessor
from thenvoi.runtime.execution import ExecutionContext

logger = logging.getLogger(__name__)

_fh = logging.FileHandler("decision_debug.log")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logger.addHandler(_fh)
logger.setLevel(logging.DEBUG)

_decision_barrier: dict[str, set[str]] = {}


def _mention_required() -> bool:
    return os.getenv("BAND_REQUIRE_MENTION", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }


class MentionGatePreprocessor(DefaultPreprocessor):
    """DefaultPreprocessor that ignores chat messages not addressed to this agent."""

    def __init__(self, config_key: str | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config_key = config_key

    async def process(self, ctx: ExecutionContext, event: PlatformEvent, agent_id: str):
        if not _mention_required() or not isinstance(event, MessageEvent):
            return await super().process(ctx, event, agent_id)

        msg = event.payload
        if msg is None:
            return await super().process(ctx, event, agent_id)

        is_self = msg.sender_type == "Agent" and msg.sender_id == agent_id
        if is_self:
            return await super().process(ctx, event, agent_id)

        mention_ids = [m.id for m in (msg.metadata.mentions if msg.metadata else [])]
        if agent_id not in mention_ids:
            logger.debug(
                "Room %s: skipping message %s — agent %s not mentioned",
                getattr(event, "room_id", "?"),
                msg.id,
                agent_id,
            )
            return None

        if self.config_key == "decision_agent" and msg.sender_type == "Agent":
            content = msg.content or ""
            batch_match = re.search(r"BATCH-[A-Za-z0-9_-]+", content)
            if batch_match:
                batch_id = batch_match.group(0)
                if batch_id not in _decision_barrier:
                    _decision_barrier[batch_id] = set()
                _decision_barrier[batch_id].add(msg.sender_id)
                if len(_decision_barrier[batch_id]) < 4:
                    logger.debug(
                        "Decision Agent waiting: %s (got %d/4). Returning None.",
                        batch_id,
                        len(_decision_barrier[batch_id]),
                    )
                    return None
                else:
                    logger.info(
                        "Decision Agent barrier cleared for %s (all 4 agents reported). Passing event to adapter.",
                        batch_id,
                    )
            else:
                logger.debug(
                    "Decision Agent ignoring agent message with no batch_id. Returning None."
                )
                return None

        logger.debug(
            "Passing event %s to base preprocessor for agent %s",
            getattr(event, "id", "?"),
            agent_id,
        )
        return await super().process(ctx, event, agent_id)
