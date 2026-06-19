from band.SafeSendLangGraphAdapter import (
    SafeSendLangGraphAdapter,
    WrappedAgentTools,
    _PARTICIPANTS,
    _extract_mention_handles,
)
import logging

logger = logging.getLogger("nexusmesh.intake.adapter")


class IntakeLangGraphAdapter(SafeSendLangGraphAdapter):
    """Custom adapter for the Intake Agent that automatically appends mentions
    to the final handoff message so the LLM doesn't have to remember them.
    """

    async def on_message(self, msg, tools, *args, **kwargs) -> None:
        self._captured_ai_content = ""
        self._fallback_mentions = self._resolve_fallback_mentions(msg)
        wrapped_tools = WrappedAgentTools(
            tools, fallback_mentions=self._fallback_mentions
        )

        await super(SafeSendLangGraphAdapter, self).on_message(
            msg, wrapped_tools, *args, **kwargs
        )

        final_content = (
            wrapped_tools.explicit_content
            if wrapped_tools.explicit_content
            else self._captured_ai_content
        )

        if final_content:
            if "[batch_id=" in final_content:
                analysis_agents = [
                    "@tarun.mukku/fraud-agent",
                    "@tarun.mukku/doc-authenticity-agent",
                    "@tarun.mukku/regulatory-agent",
                    "@tarun.mukku/policy-agent",
                ]
                for m in analysis_agents:
                    if m not in final_content:
                        final_content = f"{m} " + final_content

            if wrapped_tools.explicit_mentions:
                mentions = wrapped_tools.explicit_mentions
            else:
                mentions = _extract_mention_handles(final_content, _PARTICIPANTS)
                if not mentions:
                    mentions = self._fallback_mentions

            if "[batch_id=" in final_content:
                for m in analysis_agents:
                    if m not in mentions:
                        mentions.append(m)

            if not mentions:
                mentions = ["tarun.mukku"]

            logger.info(
                "IntakeAgent auto-sending message directly via code (%d chars)",
                len(final_content),
            )
            await tools.send_message(content=final_content, mentions=mentions)
        else:
            logger.warning("IntakeAgent generated no content to send.")
