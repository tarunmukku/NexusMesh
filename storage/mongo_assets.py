"""GridFS-backed asset store + reference-collection helpers (single source of truth)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

ASSET_BUCKET = "assets"

KIND_CLAIM_IMAGE = "claim_image"
KIND_POLICY_PDF = "policy_pdf"
KIND_BENCHMARK_IMAGE = "benchmark_image"
KIND_DOCUMENT = "document"

COLL_FRAUD_DB = "fraud_db"
COLL_RING = "ring_relationships"
COLL_INVESTIGATION = "investigation_history"
COLL_NAIC = "naic_cache"
COLL_OFAC = "ofac_sdn"
COLL_CLAIM_CASES = "claim_cases"

_SUFFIX_BY_KIND = {
    KIND_CLAIM_IMAGE: ".jpg",
    KIND_BENCHMARK_IMAGE: ".jpg",
    KIND_POLICY_PDF: ".pdf",
}


def suffix_for(filename: str | None, kind: str | None = None) -> str:
    if filename:
        suf = Path(filename).suffix
        if suf:
            return suf
    return _SUFFIX_BY_KIND.get(kind or "", "")


def _resolve_db(db: Any = None, uri: str | None = None, db_name: str | None = None):
    if db is not None:
        return db
    from agents.intake_agent.data_store import get_client, DEFAULT_DB_NAME

    return get_client(uri)[db_name or DEFAULT_DB_NAME]


def _fs(db: Any, bucket: str = ASSET_BUCKET):
    import gridfs

    return gridfs.GridFS(db, collection=bucket)


def _to_object_id(asset_id: Any):
    from bson import ObjectId

    return asset_id if isinstance(asset_id, ObjectId) else ObjectId(str(asset_id))


def put_asset(
    data: bytes | str | os.PathLike,
    filename: str | None = None,
    kind: str = KIND_DOCUMENT,
    claim_id: str | None = None,
    content_type: str | None = None,
    label: str | None = None,
    extra: dict[str, Any] | None = None,
    *,
    db: Any = None,
    uri: str | None = None,
    db_name: str | None = None,
    bucket: str = ASSET_BUCKET,
) -> str:
    if isinstance(data, (str, os.PathLike)):
        path = Path(data)
        content = path.read_bytes()
        filename = filename or path.name
    else:
        content = data
    if not filename:
        raise ValueError("filename is required when data is raw bytes")

    fs = _fs(_resolve_db(db, uri, db_name), bucket)
    # de-dupe: drop any prior file with same kind+filename
    for old in fs.find({"kind": kind, "filename": filename}):
        fs.delete(old._id)

    meta: dict[str, Any] = {"filename": filename, "kind": kind}
    if claim_id:
        meta["claim_id"] = claim_id
    if content_type:
        meta["contentType"] = content_type
    if label:
        meta["label"] = label
    if extra:
        meta.update(extra)
    return str(fs.put(content, **meta))


def get_asset_bytes(
    asset_id: Any = None,
    *,
    query: dict[str, Any] | None = None,
    db: Any = None,
    uri: str | None = None,
    db_name: str | None = None,
    bucket: str = ASSET_BUCKET,
) -> bytes:
    fs = _fs(_resolve_db(db, uri, db_name), bucket)
    if asset_id is not None:
        return fs.get(_to_object_id(asset_id)).read()
    gout = fs.find_one(query or {})
    if gout is None:
        raise FileNotFoundError(f"No asset matching {query!r} in bucket {bucket!r}")
    return gout.read()


def resolve_to_tempfile(
    asset_id: Any = None,
    *,
    query: dict[str, Any] | None = None,
    suffix: str | None = None,
    kind: str | None = None,
    db: Any = None,
    uri: str | None = None,
    db_name: str | None = None,
    bucket: str = ASSET_BUCKET,
) -> str:
    fs = _fs(_resolve_db(db, uri, db_name), bucket)
    if asset_id is not None:
        gout = fs.get(_to_object_id(asset_id))
    else:
        gout = fs.find_one(query or {})
        if gout is None:
            raise FileNotFoundError(f"No asset matching {query!r} in bucket {bucket!r}")
    data = gout.read()
    if suffix is None:
        suffix = suffix_for(
            getattr(gout, "filename", None), kind or getattr(gout, "kind", None)
        )
    fd, path = tempfile.mkstemp(suffix=suffix or "")
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return path


def _asset_summary(gout: Any) -> dict[str, Any]:
    return {
        "asset_id": str(gout._id),
        "filename": getattr(gout, "filename", None),
        "kind": getattr(gout, "kind", None),
        "claim_id": getattr(gout, "claim_id", None),
        "label": getattr(gout, "label", None),
        "length": getattr(gout, "length", None),
    }


def find_assets(
    *,
    db: Any = None,
    uri: str | None = None,
    db_name: str | None = None,
    bucket: str = ASSET_BUCKET,
    **filters: Any,
) -> list[dict[str, Any]]:
    fs = _fs(_resolve_db(db, uri, db_name), bucket)
    return [_asset_summary(f) for f in fs.find(filters)]


def find_one_asset(
    *,
    db: Any = None,
    uri: str | None = None,
    db_name: str | None = None,
    bucket: str = ASSET_BUCKET,
    **filters: Any,
) -> dict[str, Any] | None:
    fs = _fs(_resolve_db(db, uri, db_name), bucket)
    gout = fs.find_one(filters)
    return _asset_summary(gout) if gout is not None else None


def upsert_documents(
    collection: str,
    docs: Iterable[dict[str, Any]],
    key: str | None = None,
    drop: bool = False,
    *,
    db: Any = None,
    uri: str | None = None,
    db_name: str | None = None,
) -> int:
    col = _resolve_db(db, uri, db_name)[collection]
    docs = [dict(d) for d in docs]
    if drop:
        col.delete_many({})
    if key:
        for d in docs:
            if d.get(key) is not None:
                col.replace_one({key: d.get(key)}, d, upsert=True)
    elif docs:
        col.insert_many(docs)
    return col.count_documents({})


def load_documents(
    collection: str,
    query: dict[str, Any] | None = None,
    *,
    db: Any = None,
    uri: str | None = None,
    db_name: str | None = None,
) -> list[dict[str, Any]]:
    col = _resolve_db(db, uri, db_name)[collection]
    out = []
    for d in col.find(query or {}):
        d.pop("_id", None)
        out.append(d)
    return out
