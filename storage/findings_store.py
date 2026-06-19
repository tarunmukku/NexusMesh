"""Shared agent-findings store (MongoDB)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agents.intake_agent.data_store import get_collection, DEFAULT_DB_NAME

DEFAULT_FINDINGS_COLLECTION = "agent_findings"


def _coll(
    uri: str | None = None,
    db_name: str = DEFAULT_DB_NAME,
    collection: str = DEFAULT_FINDINGS_COLLECTION,
):
    col = get_collection(uri, db_name, collection)
    col.create_index(
        [("batch_id", 1), ("message_type", 1), ("claim_id", 1)], unique=True
    )
    return col


def write_findings(
    batch_id: str,
    message_type: str,
    payload: Any,
    *,
    claim_id: str | None = None,
    summary: str | None = None,
    agent: str | None = None,
    uri: str | None = None,
    db_name: str = DEFAULT_DB_NAME,
    collection: str = DEFAULT_FINDINGS_COLLECTION,
) -> dict[str, Any]:
    col = _coll(uri, db_name, collection)
    key = {"batch_id": batch_id, "message_type": message_type, "claim_id": claim_id}
    doc = dict(key)
    doc.update(
        {
            "agent": agent,
            "summary": summary,
            "payload": payload,
            "_ingested_at": datetime.now(timezone.utc),
        }
    )
    col.replace_one(key, doc, upsert=True)
    return {
        "batch_id": batch_id,
        "message_type": message_type,
        "claim_id": claim_id,
        "persisted_to": f"{db_name}.{collection}",
    }


def read_findings(
    batch_id: str,
    message_type: str,
    *,
    claim_id: str | None = None,
    uri: str | None = None,
    db_name: str = DEFAULT_DB_NAME,
    collection: str = DEFAULT_FINDINGS_COLLECTION,
) -> Any | None:
    col = _coll(uri, db_name, collection)
    doc = col.find_one(
        {"batch_id": batch_id, "message_type": message_type, "claim_id": claim_id},
        {"_id": 0},
    )
    return doc.get("payload") if doc else None


def list_findings(
    batch_id: str,
    *,
    uri: str | None = None,
    db_name: str = DEFAULT_DB_NAME,
    collection: str = DEFAULT_FINDINGS_COLLECTION,
) -> list[dict[str, Any]]:
    col = _coll(uri, db_name, collection)
    out = []
    for d in col.find({"batch_id": batch_id}, {"_id": 0, "payload": 0}):
        ts = d.get("_ingested_at")
        if isinstance(ts, datetime):
            d["_ingested_at"] = ts.isoformat()
        out.append(d)
    out.sort(key=lambda d: (d.get("message_type") or "", d.get("claim_id") or ""))
    return out
