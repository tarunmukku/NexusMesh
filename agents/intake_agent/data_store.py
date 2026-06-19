"""MongoDB-backed claims store for the Intake Agent."""

from __future__ import annotations

import os
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from agents.intake_agent.tools import (
    DEFAULT_CLAIMS_CSV,
    REQUIRED_COLUMNS,
    _normalize_claim,
    build_claim_manifest,
    build_stream_manifests,
    compute_batch_id,
)

DEFAULT_DB_NAME = "nexusmesh_guard"
DEFAULT_COLLECTION = "claims"
DEFAULT_MANIFEST_COLLECTION = "claim_manifests"
DEFAULT_CLUSTER_HOST = "clusternexusmesh.q1zyzza.mongodb.net"
DEFAULT_APP_NAME = "ClusterNexusMesh"
_SCHEMA_FIELDS = list(REQUIRED_COLUMNS)


def build_mongo_uri(uri: str | None = None) -> str:
    """Resolve the MongoDB connection string from one source of truth."""
    uri = uri or os.getenv("MONGODB_URI")
    if uri:
        return uri

    user = os.getenv("MONGODB_USERNAME")
    password = os.getenv("MONGODB_PASSWORD")
    if user and password:
        host = os.getenv("MONGODB_CLUSTER_HOST", DEFAULT_CLUSTER_HOST)
        app = os.getenv("MONGODB_APP_NAME", DEFAULT_APP_NAME)
        u = urllib.parse.quote_plus(user)
        p = urllib.parse.quote_plus(password)
        return (
            f"mongodb+srv://{u}:{p}@{host}/"
            f"?retryWrites=true&w=majority&appName={app}"
        )

    raise RuntimeError(
        "MongoDB credentials not set. Provide either MONGODB_URI, or "
        "MONGODB_USERNAME + MONGODB_PASSWORD (optionally MONGODB_CLUSTER_HOST / "
        "MONGODB_APP_NAME) in .env."
    )


_resolve_uri = build_mongo_uri


def get_client(uri: str | None = None, server_timeout_ms: int = 8000):
    """Return a connected MongoClient. Requires pymongo (pymongo[srv])."""
    from pymongo import MongoClient

    client = MongoClient(
        build_mongo_uri(uri), serverSelectionTimeoutMS=server_timeout_ms
    )
    client.admin.command("ping")
    return client


def get_collection(
    uri: str | None = None,
    db_name: str = DEFAULT_DB_NAME,
    collection: str = DEFAULT_COLLECTION,
):
    return get_client(uri)[db_name][collection]


ASSET_REF_FIELDS = ("image_ref", "policy_ref")


def _project_claim(doc: dict[str, Any]) -> dict[str, Any]:
    """Keep the claim schema fields, plus any asset references, from a stored doc."""
    out = {k: doc.get(k) for k in _SCHEMA_FIELDS}
    for k in ASSET_REF_FIELDS:
        ref = doc.get(k)
        if ref is not None:
            out[k] = ref
    return out


def _manifest_from_docs(
    docs: Iterable[dict[str, Any]], batch_id: str | None = None
) -> dict[str, Any]:
    """Build a claim_manifest from raw Mongo documents. Pure / offline."""
    claims = [_project_claim(d) for d in docs]
    claims.sort(key=lambda c: (c.get("claim_id") or ""))
    return build_claim_manifest(claims, batch_id=batch_id)


def _stream_manifests_from_docs(
    docs: Iterable[dict[str, Any]], batch_id: str | None = None
) -> list[dict[str, Any]]:
    """Build per-claim manifests (shared batch_id) from raw Mongo docs. Pure / offline."""
    claims = [_project_claim(d) for d in docs]
    return build_stream_manifests(claims, batch_id=batch_id)


def seed_claims_from_csv(
    csv_path: str | os.PathLike | None = None,
    uri: str | None = None,
    db_name: str = DEFAULT_DB_NAME,
    collection: str = DEFAULT_COLLECTION,
    drop: bool = False,
) -> dict[str, Any]:
    import csv as _csv

    path = Path(csv_path) if csv_path else DEFAULT_CLAIMS_CSV
    if not path.exists():
        raise FileNotFoundError(f"Claims CSV not found: {path}")

    seed_id = "SEED-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    now = datetime.now(timezone.utc)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = [
            r for r in _csv.DictReader(fh) if any((v or "").strip() for v in r.values())
        ]
    claims = [_normalize_claim(r) for r in rows]

    col = get_collection(uri, db_name, collection)
    if drop:
        col.delete_many({})
    col.create_index("claim_id", unique=True)

    from pymongo import ReplaceOne

    ops = []
    for c in claims:
        doc = dict(c)
        doc["_source"] = str(path.name)
        doc["_seed_id"] = seed_id
        doc["_ingested_at"] = now
        ops.append(ReplaceOne({"claim_id": c["claim_id"]}, doc, upsert=True))
    result = col.bulk_write(ops, ordered=False)

    return {
        "seed_id": seed_id,
        "db": db_name,
        "collection": collection,
        "rows_in_csv": len(claims),
        "upserts": result.upserted_count,
        "modified": result.modified_count,
        "total_in_collection": col.count_documents({}),
    }


def fetch_claims(
    uri: str | None = None,
    db_name: str = DEFAULT_DB_NAME,
    collection: str = DEFAULT_COLLECTION,
    query: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return claim documents (schema-projected) from MongoDB."""
    col = get_collection(uri, db_name, collection)
    docs = list(col.find(query or {}))
    return [_project_claim(d) for d in docs]


def build_manifest_from_mongo(
    uri: str | None = None,
    db_name: str = DEFAULT_DB_NAME,
    collection: str = DEFAULT_COLLECTION,
    query: dict[str, Any] | None = None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    docs = fetch_claims(uri, db_name, collection, query)
    if not docs:
        raise RuntimeError(
            f"No claims found in {db_name}.{collection}. Run scripts/seed_mongo.py first."
        )
    return _manifest_from_docs(docs, batch_id=batch_id)


def list_claim_ids(
    uri: str | None = None,
    db_name: str = DEFAULT_DB_NAME,
    collection: str = DEFAULT_COLLECTION,
    query: dict[str, Any] | None = None,
) -> list[str]:
    """Return the ordered claim_ids in the collection (no full docs fetched)."""
    col = get_collection(uri, db_name, collection)
    docs = col.find(query or {}, {"claim_id": 1, "_id": 0})
    ids = [d.get("claim_id") for d in docs if d.get("claim_id")]
    return sorted(ids)


def stream_manifests_from_mongo(
    uri: str | None = None,
    db_name: str = DEFAULT_DB_NAME,
    collection: str = DEFAULT_COLLECTION,
    query: dict[str, Any] | None = None,
    batch_id: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch claims from MongoDB and build one per-claim manifest each (shared batch_id)."""
    docs = fetch_claims(uri, db_name, collection, query)
    if not docs:
        raise RuntimeError(
            f"No claims found in {db_name}.{collection}. Run scripts/seed_mongo.py first."
        )
    return _stream_manifests_from_docs(docs, batch_id=batch_id)


def _manifest_summary(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact summary of a per-claim stream — small enough to post/return cheaply."""
    claim_ids, states, loss_types = [], set(), set()
    batch_id = manifests[0]["batch_id"] if manifests else None
    for m in manifests:
        claim = (m.get("claims") or [{}])[0]
        if claim.get("claim_id"):
            claim_ids.append(claim["claim_id"])
        if claim.get("state"):
            states.add(claim["state"])
        if claim.get("loss_type"):
            loss_types.add(claim["loss_type"])
    return {
        "batch_id": batch_id,
        "batch_size": len(manifests),
        "claim_ids": sorted(claim_ids),
        "states_represented": sorted(states),
        "loss_types": sorted(loss_types),
    }


def write_stream_manifests(
    manifests: list[dict[str, Any]],
    uri: str | None = None,
    db_name: str = DEFAULT_DB_NAME,
    collection: str = DEFAULT_MANIFEST_COLLECTION,
) -> dict[str, Any]:
    """Upsert per-claim manifests into the claim_manifests collection."""
    from pymongo import ReplaceOne

    col = get_collection(uri, db_name, collection)
    col.create_index([("batch_id", 1), ("claim_id", 1)], unique=True)
    now = datetime.now(timezone.utc)

    ops = []
    for m in manifests:
        claim_id = (m.get("claims") or [{}])[0].get("claim_id")
        doc = dict(m)
        doc["claim_id"] = claim_id
        doc["_ingested_at"] = now
        ops.append(
            ReplaceOne(
                {"batch_id": m.get("batch_id"), "claim_id": claim_id}, doc, upsert=True
            )
        )
    if ops:
        col.bulk_write(ops, ordered=False)
    return _manifest_summary(manifests)


def is_batch_persisted(
    batch_id: str,
    uri: str | None = None,
    db_name: str = DEFAULT_DB_NAME,
    collection: str = DEFAULT_MANIFEST_COLLECTION,
) -> dict[str, Any]:
    """Recovery check: has this batch_id already been written to claim_manifests?"""
    col = get_collection(uri, db_name, collection)
    count = col.count_documents({"batch_id": batch_id})
    return {"batch_id": batch_id, "persisted": count > 0, "count": count}


def persist_stream_from_mongo(
    uri: str | None = None,
    db_name: str = DEFAULT_DB_NAME,
    claims_collection: str = DEFAULT_COLLECTION,
    manifest_collection: str = DEFAULT_MANIFEST_COLLECTION,
    query: dict[str, Any] | None = None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Build per-claim manifests from the claims store and persist them."""
    docs = fetch_claims(uri, db_name, claims_collection, query)
    if not docs:
        raise RuntimeError(
            f"No claims found in {db_name}.{claims_collection}. "
            "Run scripts/seed_mongo.py first."
        )
    if batch_id is None:
        batch_id = compute_batch_id([d.get("claim_id") for d in docs])

    already = is_batch_persisted(batch_id, uri, db_name, manifest_collection)
    manifests = _stream_manifests_from_docs(docs, batch_id=batch_id)
    if not (already["persisted"] and already["count"] >= len(manifests)):
        write_stream_manifests(manifests, uri, db_name, manifest_collection)

    summary = _manifest_summary(manifests)
    summary["persisted_to"] = f"{db_name}.{manifest_collection}"
    summary["already_persisted"] = bool(
        already["persisted"] and already["count"] >= len(manifests)
    )
    return summary


def read_stream_manifests(
    batch_id: str,
    uri: str | None = None,
    db_name: str = DEFAULT_DB_NAME,
    collection: str = DEFAULT_MANIFEST_COLLECTION,
) -> list[dict[str, Any]]:
    col = get_collection(uri, db_name, collection)
    docs = list(col.find({"batch_id": batch_id}))
    manifests = []
    for d in docs:
        d.pop("_id", None)
        d.pop("_ingested_at", None)
        manifests.append(d)
    manifests.sort(key=lambda m: m.get("claim_index") or 0)
    return manifests
