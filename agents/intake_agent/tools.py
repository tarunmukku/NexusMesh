"""Intake Agent tools (Agent 1)."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "data"
DEFAULT_CLAIMS_CSV = DATA_DIR / "sample_claims.csv"

VALID_LOSS_TYPES = {
    "collision",
    "comprehensive",
    "theft",
    "vandalism",
    "fire",
    "weather",
    "bi_liability",
    "pd_liability",
    "pip",
    "medpay",
    "umuim",
}

REQUIRED_COLUMNS = [
    "claim_id",
    "claimant_id",
    "policy_number",
    "policy_inception_date",
    "loss_date",
    "loss_type",
    "claimed_amount",
    "state",
    "provider",
    "tow_company",
    "has_attached_image",
]
OPTIONAL_NULLABLE = {"tow_company", "provider"}


def _none_if_blank(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    return v if v else None


def _to_bool(value: str | None) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y", "t"}


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    v = str(value).strip().replace("$", "").replace(",", "")
    if not v:
        return None
    try:
        return round(float(v), 2)
    except ValueError:
        return None


def _normalize_claim(row: dict[str, Any]) -> dict[str, Any]:
    """Coerce one raw CSV row into the typed claim schema. Missing -> null."""
    g = lambda k: _none_if_blank(row.get(k))
    loss_type = (g("loss_type") or "").lower() or None
    if loss_type is not None and loss_type not in VALID_LOSS_TYPES:
        loss_type = loss_type
    state = g("state") or ""
    state = state.upper()[:2] if state else None
    return {
        "claim_id": g("claim_id"),
        "claimant_id": g("claimant_id"),
        "policy_number": g("policy_number"),
        "policy_inception_date": g("policy_inception_date"),
        "loss_date": g("loss_date"),
        "loss_type": loss_type,
        "claimed_amount": _to_float(row.get("claimed_amount")),
        "state": state,
        "provider": g("provider"),
        "tow_company": g("tow_company"),
        "has_attached_image": _to_bool(row.get("has_attached_image")),
    }


def _make_batch_id() -> str:
    return "BATCH-" + datetime.now(timezone.utc).strftime("%Y-%m%d-%H%M%S")


def compute_batch_id(claim_ids: list[str]) -> str:
    """Deterministic batch_id derived from the claim set (stable across re-runs)."""
    ids = sorted(c for c in claim_ids if c)
    if not ids:
        return _make_batch_id()
    digest = hashlib.sha256("|".join(ids).encode("utf-8")).hexdigest()[:10]
    return f"BATCH-{digest}"


def build_claim_manifest(
    claims: list[dict[str, Any]], batch_id: str | None = None
) -> dict[str, Any]:
    """Assemble the claim_manifest payload from a list of normalized claims."""
    states = sorted({c["state"] for c in claims if c.get("state")})
    loss_types = sorted({c["loss_type"] for c in claims if c.get("loss_type")})
    return {
        "message_type": "claim_manifest",
        "batch_id": batch_id or _make_batch_id(),
        "total_claims": len(claims),
        "states_represented": states,
        "loss_types": loss_types,
        "claims": claims,
    }


def build_stream_manifests(
    claims: list[dict[str, Any]], batch_id: str | None = None
) -> list[dict[str, Any]]:
    """Build a list of per-claim claim_manifests that all share one batch_id."""
    batch_id = batch_id or _make_batch_id()
    ordered = sorted(claims, key=lambda c: (c.get("claim_id") or ""))
    batch_size = len(ordered)
    manifests: list[dict[str, Any]] = []
    for idx, claim in enumerate(ordered, start=1):
        m = build_claim_manifest([claim], batch_id=batch_id)
        m["batch_size"] = batch_size
        m["claim_index"] = idx
        manifests.append(m)
    return manifests


def parse_claims_csv(csv_path: str | os.PathLike | None = None) -> dict[str, Any]:
    """Read a claims CSV and return a claim_manifest dict. Pure / offline."""
    path = Path(csv_path) if csv_path else DEFAULT_CLAIMS_CSV
    if not path.exists():
        raise FileNotFoundError(f"Claims CSV not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        rows = [r for r in reader if any((v or "").strip() for v in r.values())]
    claims = [_normalize_claim(r) for r in rows]
    batch_id = compute_batch_id([c.get("claim_id") for c in claims])
    return build_claim_manifest(claims, batch_id=batch_id)


def read_image_metadata(image_path: str | os.PathLike) -> dict[str, Any]:
    """Extract EXIF fields relevant to forensics (best-effort, optional dep)."""
    meta: dict[str, Any] = {"exif_available": False}
    try:
        import piexif  # type: ignore

        exif = piexif.load(str(image_path))
        zeroth, exif_ifd, gps = (
            exif.get("0th", {}),
            exif.get("Exif", {}),
            exif.get("GPS", {}),
        )

        def dec(d, key):
            v = d.get(key)
            return v.decode(errors="ignore") if isinstance(v, bytes) else v

        meta = {
            "exif_available": True,
            "camera_make": dec(zeroth, piexif.ImageIFD.Make),
            "camera_model": dec(zeroth, piexif.ImageIFD.Model),
            "software": dec(zeroth, piexif.ImageIFD.Software),
            "datetime_original": dec(exif_ifd, piexif.ExifIFD.DateTimeOriginal),
            "has_gps": bool(gps),
        }
    except Exception as exc:  # noqa: BLE001
        meta["exif_error"] = str(exc)
    return meta


def _extract_fields_via_vision(image_path: str | os.PathLike) -> dict[str, Any]:
    """Use the AIML vision LLM (Gemini) to read claim fields off an image."""
    from llm.client import aiml_llm
    from langchain_core.messages import HumanMessage

    with open(image_path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()

    instruction = (
        "You are extracting fields from a US auto-insurance claim form image. "
        "Return ONLY a JSON object with these keys (use null if a field is absent, "
        "never guess): claim_id, claimant_id, policy_number, policy_inception_date, "
        "loss_date, loss_type, claimed_amount, state, provider, tow_company, "
        "has_attached_image. Dates ISO YYYY-MM-DD; claimed_amount as a number; "
        "loss_type lowercased."
    )
    llm = aiml_llm("google/gemini-2.5-flash")
    msg = HumanMessage(
        content=[
            {"type": "text", "text": instruction},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            },
        ]
    )
    resp = llm.invoke([msg])
    text = resp.content if isinstance(resp.content, str) else str(resp.content)
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :]
    try:
        return json.loads(text[text.find("{") : text.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        return {"_raw_vision_output": text}


@tool
def parse_csv_tool(csv_path: str = "") -> str:
    """Parse a claims CSV batch into a NexusMesh Guard claim_manifest."""
    manifest = parse_claims_csv(csv_path or None)
    return json.dumps(manifest, indent=2)


@tool
def fetch_claims_tool(claim_id: str = "") -> str:
    """Fetch the claims batch from the MongoDB cluster and build the claim_manifest."""
    from agents.intake_agent.data_store import build_manifest_from_mongo

    query = {"claim_id": claim_id.strip()} if claim_id and claim_id.strip() else None
    try:
        manifest = build_manifest_from_mongo(query=query)
        return json.dumps(manifest, indent=2)
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "error": f"MongoDB fetch failed: {exc}",
                "fallback": "Call parse_csv_tool to read data/sample_claims.csv instead.",
            }
        )


@tool
def list_claim_ids_tool() -> str:
    """List the claim_ids in the MongoDB batch and open a streaming batch_id."""
    from agents.intake_agent.data_store import list_claim_ids

    try:
        claim_ids = list_claim_ids()
        return json.dumps(
            {
                "batch_id": _make_batch_id(),
                "total_claims": len(claim_ids),
                "claim_ids": claim_ids,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "error": f"MongoDB list failed: {exc}",
                "fallback": "Call parse_csv_tool to read data/sample_claims.csv instead.",
            }
        )


@tool
def stream_claims_tool() -> str:
    """Build one claim_manifest PER claim for the whole batch (streaming FNOL flow)."""
    from agents.intake_agent.data_store import stream_manifests_from_mongo

    try:
        manifests = stream_manifests_from_mongo()
        batch_id = manifests[0]["batch_id"] if manifests else _make_batch_id()
        return json.dumps(
            {
                "batch_id": batch_id,
                "batch_size": len(manifests),
                "manifests": manifests,
            },
            indent=2,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "error": f"MongoDB stream failed: {exc}",
                "fallback": "Call parse_csv_tool to read data/sample_claims.csv instead.",
            }
        )


@tool
def persist_claim_stream_tool() -> str:
    """Persist one per-claim FNOL manifest per claim to the shared store, fast."""
    from agents.intake_agent.data_store import persist_stream_from_mongo

    try:
        summary = persist_stream_from_mongo()
        return json.dumps(summary)
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "error": f"MongoDB persist failed: {exc}",
                "fallback": "Call parse_csv_tool to read data/sample_claims.csv instead.",
            }
        )


@tool
def check_batch_status_tool(batch_id: str) -> str:
    """Check whether a batch_id has already been persisted (crash-recovery check)."""
    from agents.intake_agent.data_store import is_batch_persisted

    try:
        return json.dumps(is_batch_persisted(batch_id.strip()))
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {"error": f"MongoDB status check failed: {exc}", "batch_id": batch_id}
        )


@tool
def ocr_image_tool(image_path: str) -> str:
    """Extract claim fields from a scanned claim form or damage photo via vision."""
    if not Path(image_path).exists():
        return json.dumps({"error": f"Image not found: {image_path}"})
    fields = _extract_fields_via_vision(image_path)
    metadata = read_image_metadata(image_path)
    return json.dumps({"fields": fields, "image_metadata": metadata}, indent=2)


INTAKE_TOOLS = [
    persist_claim_stream_tool,
    check_batch_status_tool,
    stream_claims_tool,
    list_claim_ids_tool,
    fetch_claims_tool,
    parse_csv_tool,
    ocr_image_tool,
]
