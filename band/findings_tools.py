"""Shared LangChain tools for the Mongo-backed agent_findings store."""
from __future__ import annotations

import json

from langchain_core.tools import tool


@tool
def read_findings_tool(batch_id: str, message_type: str) -> str:
    """Pull another agent's full findings JSON for a batch from the shared store."""
    try:
        from storage.findings_store import read_findings
        payload = read_findings(batch_id.strip(), message_type.strip())
        if payload is None:
            return json.dumps({"error": f"No {message_type} stored for batch {batch_id}."})
        return json.dumps(payload, indent=2)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"findings store read failed: {exc}"})


@tool
def list_findings_tool(batch_id: str) -> str:
    """List which agent findings are available in the shared store for a batch."""
    try:
        from storage.findings_store import list_findings
        return json.dumps({"batch_id": batch_id, "available": list_findings(batch_id.strip())},
                          indent=2)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"findings store list failed: {exc}"})


FINDINGS_READ_TOOLS = [read_findings_tool, list_findings_tool]
