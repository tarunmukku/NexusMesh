import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from thenvoi import Agent
from thenvoi.adapters import LangGraphAdapter
from thenvoi.config import load_agent_config
from thenvoi.core.types import PlatformMessage
from thenvoi.core.protocols import AgentToolsProtocol

if __package__ is None and __file__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.intake_agent import INTAKE_SYSTEM_PROMPT, INTAKE_TOOLS
from agents.intake_agent.adapter import IntakeLangGraphAdapter
from agents.doc_authenticity_agent import DOC_AUTH_SYSTEM_PROMPT, DOC_AUTH_TOOLS
from agents.fraud_agent import FRAUD_SYSTEM_PROMPT, FRAUD_TOOLS
from agents.regulatory_agent import REGULATORY_SYSTEM_PROMPT, REGULATORY_TOOLS
from agents.policy_agent import POLICY_SYSTEM_PROMPT, POLICY_TOOLS
from agents.decision_agent import (
    DECISION_SYSTEM_PROMPT,
    DECISION_TOOLS,
    DecisionLangGraphAdapter,
)
from band.mention_gate import MentionGatePreprocessor
from band.findings_tools import FINDINGS_READ_TOOLS
from band.SafeSendLangGraphAdapter import SafeSendLangGraphAdapter, WrappedAgentTools

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nexusmesh")

DECISION_INTERIM_PROMPT = """\
You are the Decision Agent for NexusMesh Guard. You issue the final approve / decline /
escalate verdict for a claims batch and route human-in-the-loop (HITL) review.

The other agents no longer paste their JSON into the room — they persist it to the shared
store. When the Fraud Agent @mentions you with a batch_id (e.g. from
"[INTAKE_COMPLETE batch_id=BATCH-xxxx]" or its own summary), you MUST pull the real data
before deciding:
  1. Call `list_findings_tool(batch_id)` to see what's available.
  2. Call `read_findings_tool(batch_id, "fraud_findings")` for the per-claim scores, tiers,
     rings, and reason codes. Also pull `read_findings_tool(batch_id, "doc_authenticity")`
     when image authenticity matters.
Base your verdict ONLY on what you read back — never invent claim ids, scores, or rings.

Apply traffic-light routing: 🟢 GREEN auto-approve (straight-through), 🟡 AMBER → SIU/
adjuster review, 🔴 RED → decline + mandatory HITL escalation. Put RED ring members and any
OFAC/sanctions hit on a payment hold until cleared. Cite the fraud reason codes and any
history precedents in your rationale.

Post ONE concise verdict message to the room (per-claim disposition + HITL routing). Keep it
readable — no raw JSON. @mention the user for the HITL approvals on RED claims.
"""


AGENTS = {
    "intake_agent": (
        "gemini-2.5-flash",
        INTAKE_SYSTEM_PROMPT,
        INTAKE_TOOLS,
    ),
    "doc_authenticity_agent": (
        "alibaba/qwen3.5-omni-plus",
        DOC_AUTH_SYSTEM_PROMPT,
        DOC_AUTH_TOOLS,
    ),
    "fraud_agent": (
        "minimax/minimax-m3",
        FRAUD_SYSTEM_PROMPT,
        FRAUD_TOOLS,
    ),
    "regulatory_agent": (
        "x-ai/grok-4-3",
        REGULATORY_SYSTEM_PROMPT,
        REGULATORY_TOOLS,
    ),
    "policy_agent": (
        "gpt-5.1-2025-11-13",
        POLICY_SYSTEM_PROMPT,
        POLICY_TOOLS,
    ),
    "decision_agent": (
        "openai/gpt-5-2-chat-latest",
        DECISION_SYSTEM_PROMPT,
        DECISION_TOOLS,
    ),
}


def get_agent_handlers() -> dict[str, str]:
    import yaml
    from pathlib import Path

    for p in [
        Path("agent_config.yaml"),
        Path(__file__).resolve().parent.parent / "agent_config.yaml",
    ]:
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f)
                    return {
                        k: v.get("handler")
                        for k, v in cfg.items()
                        if isinstance(v, dict) and v.get("handler")
                    }
            except Exception:
                pass
    return {
        "intake_agent": "@tarun.mukku/intake-agent",
        "doc_authenticity_agent": "@tarun.mukku/doc-authenticity-agent",
        "fraud_agent": "@tarun.mukku/fraud-agent",
        "regulatory_agent": "@tarun.mukku/regulatory-agent",
        "policy_agent": "@tarun.mukku/policy-agent",
        "decision_agent": "@tarun.mukku/decision-agent",
    }


def build_agent(
    config_key: str, model: str, prompt: str, tools: list | None = None
) -> Agent:
    agent_id, api_key = load_agent_config(config_key)
    if config_key == "decision_agent":

        adapter_cls = DecisionLangGraphAdapter
        adapter = adapter_cls(
            llm=ChatOpenAI(
                base_url="https://api.aimlapi.com/v1",
                api_key=os.getenv("AIML_API_KEY"),
                model=model,
            ),
            checkpointer=InMemorySaver(),
            custom_section=prompt,
            additional_tools=tools or [],
        )
    elif config_key == "intake_agent":
        adapter = IntakeLangGraphAdapter(
            llm=ChatOpenAI(
                base_url="https://api.aimlapi.com/v1",
                api_key=os.getenv("AIML_API_KEY"),
                model=model,
            ),
            checkpointer=InMemorySaver(),
            custom_section=prompt,
            additional_tools=tools or [],
        )
    else:
        adapter = SafeSendLangGraphAdapter(
            llm=ChatOpenAI(
                base_url="https://api.aimlapi.com/v1",
                api_key=os.getenv("AIML_API_KEY"),
                model=model,
            ),
            checkpointer=InMemorySaver(),
            custom_section=prompt,
            additional_tools=tools or [],
        )

    return Agent.create(
        adapter=adapter,
        agent_id=agent_id,
        api_key=api_key,
        ws_url=os.getenv("THENVOI_WS_URL"),
        rest_url=os.getenv("THENVOI_REST_URL"),
        preprocessor=MentionGatePreprocessor(config_key=config_key),
    )


async def run_forever(
    config_key: str, model: str, prompt: str, tools: list | None = None
):
    while True:
        try:
            agent = build_agent(config_key, model, prompt, tools)
            log.info(
                "Starting %s (%s) with %d custom tool(s)",
                config_key,
                model,
                len(tools or []),
            )
            await agent.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s crashed; restarting in 5s", config_key)
            await asyncio.sleep(5)


async def main():
    load_dotenv()
    tasks = [
        run_forever(key, model, prompt, tools)
        for key, (model, prompt, tools) in AGENTS.items()
    ]
    log.info("Running %d agents. Press Ctrl+C to stop.", len(tasks))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
