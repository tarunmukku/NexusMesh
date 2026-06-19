"""Document Authenticity Agent tools (Agent 2) — multi-layer image forensics."""

from __future__ import annotations

import base64
import io
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from band.findings_tools import FINDINGS_READ_TOOLS

DOC_AUTH_VISION_MODEL = os.getenv("DOC_AUTH_VISION_MODEL", "alibaba/qwen3.5-omni-plus")

_W_VISION, _W_COMPRESSION, _W_EXIF, _W_C2PA = 0.50, 0.20, 0.20, 0.10

_AI_SOFTWARE = re.compile(
    r"(firefly|midjourney|stable\s*diffusion|dall[\s-]?e|generative|gan|"
    r"photoshop|gimp|topaz|runway|leonardo|flux)",
    re.I,
)
_C2PA_MARKERS = (b"c2pa", b"jumbf", b"contentauth", b"urn:uuid:", b"adobe.com/c2pa")


def run_exif_layer(
    image_path: str | os.PathLike,
    loss_date: str | None = None,
    claimed_state: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"available": False, "flags": [], "risk": 0.0, "fields": {}}
    try:
        import piexif

        exif = piexif.load(str(image_path))
        z, e, g = exif.get("0th", {}), exif.get("Exif", {}), exif.get("GPS", {})

        def dec(d, k):
            v = d.get(k)
            return (
                v.decode(errors="ignore").strip("\x00 ") if isinstance(v, bytes) else v
            )

        make = dec(z, piexif.ImageIFD.Make)
        model = dec(z, piexif.ImageIFD.Model)
        software = dec(z, piexif.ImageIFD.Software) or ""
        dt_orig = dec(e, piexif.ExifIFD.DateTimeOriginal)
        has_gps = bool(g)
        out["available"] = bool(z or e or g)
        out["fields"] = {
            "make": make,
            "model": model,
            "software": software,
            "datetime_original": dt_orig,
            "has_gps": has_gps,
        }

        flags, risk = [], 0.0
        if not out["available"]:
            flags.append("exif_stripped")
            risk = max(risk, 0.20)
        if software and _AI_SOFTWARE.search(software):
            flags.append(f"ai_or_editor_software:{software}")
            risk = max(risk, 0.90)
        if out["available"] and not make:
            flags.append("no_camera_make")
            risk = max(risk, 0.40)
        if not has_gps and out["available"]:
            flags.append("no_gps")
            risk = max(risk, 0.15)
        if dt_orig and loss_date:
            try:
                cap = datetime.strptime(dt_orig[:10], "%Y:%m:%d").date()
                loss = datetime.fromisoformat(loss_date[:10]).date()
                if cap > loss:
                    flags.append("captured_after_loss_date")
                    risk = max(risk, 0.55)
            except Exception:  # noqa: BLE001
                pass
        out["flags"], out["risk"] = flags, round(risk, 3)
    except Exception as exc:  # noqa: BLE001
        out["flags"] = ["exif_stripped"]
        out["risk"] = 0.20
        out["error"] = str(exc)
    return out


def run_c2pa_layer(
    image_path: str | os.PathLike, exif_layer: dict | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {"has_c2pa": False, "risk": 0.0, "flags": []}
    try:
        raw = Path(image_path).read_bytes()
        out["has_c2pa"] = any(m in raw.lower() for m in _C2PA_MARKERS)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    camera_claimed = bool((exif_layer or {}).get("fields", {}).get("make"))
    if out["has_c2pa"]:
        out["flags"].append("c2pa_present")
        out["risk"] = 0.0
    elif camera_claimed:
        out["flags"].append("camera_claimed_no_c2pa")
        out["risk"] = 0.15
    else:
        out["flags"].append("no_content_credentials")
        out["risk"] = 0.10
    return out


def run_compression_layer(image_path: str | os.PathLike) -> dict[str, Any]:
    out: dict[str, Any] = {"flags": [], "risk": 0.0}
    try:
        from PIL import Image, ImageChops, ImageStat

        im = Image.open(image_path).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        buf.seek(0)
        resaved = Image.open(buf).convert("RGB")
        ela = ImageChops.difference(im, resaved)
        extrema = ela.getextrema()  # [(min,max) per band]
        max_diff = max(b[1] for b in extrema)
        mean_diff = sum(ImageStat.Stat(ela).mean) / 3.0

        out["ela_max"] = int(max_diff)
        out["ela_mean"] = round(mean_diff, 2)
        risk = 0.0
        if max_diff >= 80:
            risk = max(risk, min(1.0, (max_diff - 80) / 120.0))
            out["flags"].append(f"high_ela_peak:{int(max_diff)}")
        if mean_diff >= 8:
            risk = max(risk, min(1.0, (mean_diff - 8) / 20.0))
            out["flags"].append(f"elevated_ela_mean:{round(mean_diff,1)}")
        if not out["flags"]:
            out["flags"].append("ela_nominal")
        out["risk"] = round(risk, 3)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
        out["risk"] = 0.0
    return out


def run_vision_layer(
    image_path: str | os.PathLike, model: str | None = None
) -> dict[str, Any]:
    try:
        from llm.client import aiml_llm
        from langchain_core.messages import HumanMessage

        with open(image_path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
        instruction = (
            "You are an image forensics expert examining a car-damage photo from an insurance "
            "claim. Look for AI-generation / manipulation tells: inconsistent shadows or "
            "lighting, warped or melted edges (e.g. headlights, badges, reflections), "
            "impossible physical details, texture smearing, duplicated or cloned regions, "
            'splicing seams. Respond ONLY with JSON: {"manipulation_score": 0.0-1.0 (1.0 = '
            'almost certainly fake), "verdict": "AUTHENTIC|SUSPICIOUS|DEEPFAKE_DETECTED", '
            '"tells": [short strings]}. Be conservative; only score >0.5 with clear evidence.'
        )
        llm = aiml_llm(model or DOC_AUTH_VISION_MODEL)
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
        text = text.strip().strip("`")
        data = json.loads(text[text.find("{") : text.rfind("}") + 1])
        score = float(data.get("manipulation_score", 0.0))
        return {
            "available": True,
            "manipulation_score": max(0.0, min(1.0, score)),
            "verdict": data.get("verdict", ""),
            "tells": data.get("tells", []),
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "manipulation_score": None, "error": str(exc)}


def combine_authenticity(
    exif: dict, c2pa: dict, compression: dict, vision: dict | None
) -> dict[str, Any]:
    """Fuse the layer risks into authenticity_score (0-1) + verdict."""
    vision_available = bool(
        vision
        and vision.get("available")
        and vision.get("manipulation_score") is not None
    )
    vision_risk = float(vision["manipulation_score"]) if vision_available else 0.0

    if vision_available:
        w_v, w_c, w_e, w_p = _W_VISION, _W_COMPRESSION, _W_EXIF, _W_C2PA
    else:
        total = _W_COMPRESSION + _W_EXIF + _W_C2PA
        w_v, w_c, w_e, w_p = (
            0.0,
            _W_COMPRESSION / total,
            _W_EXIF / total,
            _W_C2PA / total,
        )

    risk = (
        w_v * vision_risk
        + w_c * compression.get("risk", 0.0)
        + w_e * exif.get("risk", 0.0)
        + w_p * c2pa.get("risk", 0.0)
    )
    if vision_available and vision_risk >= 0.85:
        risk = max(risk, vision_risk)
    if any("ai_or_editor_software" in f for f in exif.get("flags", [])):
        risk = max(risk, 0.85)
    risk = max(0.0, min(1.0, risk))
    score = round(1.0 - risk, 3)

    if score < 0.5:
        verdict = "DEEPFAKE_DETECTED"
    elif score >= 0.85 and vision_available:
        verdict = "AUTHENTIC"
    else:
        verdict = "SUSPICIOUS"

    flags = (
        list(exif.get("flags", []))
        + list(c2pa.get("flags", []))
        + list(compression.get("flags", []))
    )
    if vision_available and vision.get("tells"):
        flags += [f"vision:{t}" for t in vision["tells"]]
    if not vision_available:
        flags.append("vision_unavailable_no_confirm")
    return {
        "authenticity_score": score,
        "verdict": verdict,
        "vision_available": vision_available,
        "flags": flags,
        "recommend_review": verdict != "AUTHENTIC",
    }


def analyze_document(
    image_path: str | os.PathLike,
    claim_id: str = "",
    doc_type: str = "damage_photo",
    loss_date: str | None = None,
    run_vision: bool = True,
) -> dict[str, Any]:
    if not Path(image_path).exists():
        return {"claim_id": claim_id, "error": f"Image not found: {image_path}"}
    exif = run_exif_layer(image_path, loss_date=loss_date)
    c2pa = run_c2pa_layer(image_path, exif_layer=exif)
    comp = run_compression_layer(image_path)
    vision = (
        run_vision_layer(image_path)
        if run_vision
        else {"available": False, "manipulation_score": None}
    )
    combined = combine_authenticity(exif, c2pa, comp, vision)
    layer_evidence = []
    if combined["authenticity_score"] < 0.85:
        if vision.get("available") and vision.get("tells"):
            layer_evidence.append("vision: " + ", ".join(vision["tells"][:3]))
        if any("high_ela" in f or "elevated_ela" in f for f in comp.get("flags", [])):
            layer_evidence.append("compression/ELA anomaly")
        if any(
            "ai_or_editor" in f or "no_camera_make" in f for f in exif.get("flags", [])
        ):
            layer_evidence.append("EXIF anomaly")
    if not combined.get("vision_available"):
        layer_evidence.append(
            "vision layer unavailable — cannot confirm authenticity, review recommended"
        )
    return {
        "claim_id": claim_id,
        "doc_type": doc_type,
        "authenticity_score": combined["authenticity_score"],
        "verdict": combined["verdict"],
        "layers": {"exif": exif, "c2pa": c2pa, "compression": comp, "vision": vision},
        "flags": combined["flags"],
        "evidence": "; ".join(layer_evidence) or "all layers nominal",
        "recommend_review": combined["recommend_review"],
    }


@tool
def analyze_exif_tool(image_path: str, loss_date: str = "") -> str:
    """Layer 1: EXIF metadata forensics (camera make/model, AI/editor software, capture
    date vs loss date, GPS). Returns JSON with flags and a 0-1 risk."""
    return json.dumps(run_exif_layer(image_path, loss_date=loss_date or None), indent=2)


@tool
def check_c2pa_tool(image_path: str) -> str:
    """Layer 2: scan for C2PA / Content Credentials (JUMBF) provenance markers. Returns JSON."""
    exif = run_exif_layer(image_path)
    return json.dumps(run_c2pa_layer(image_path, exif_layer=exif), indent=2)


@tool
def detect_compression_tool(image_path: str) -> str:
    """Layer 3: JPEG Error-Level Analysis — peak/mean residual after resave; flags
    recompression or spliced/generated regions. Returns JSON."""
    return json.dumps(run_compression_layer(image_path), indent=2)


@tool
def analyze_vision_tool(image_path: str) -> str:
    return json.dumps(run_vision_layer(image_path), indent=2)


@tool
def analyze_document_tool(
    image_path: str,
    claim_id: str = "",
    doc_type: str = "damage_photo",
    loss_date: str = "",
) -> str:
    """Run the FULL multi-layer authenticity analysis (EXIF + C2PA + ELA + vision) over one
    image and return the combined doc_authenticity record (score, verdict, per-layer detail,
    flags, evidence). This is the primary tool — prefer it unless investigating one layer.
    """
    rec = analyze_document(
        image_path,
        claim_id=claim_id,
        doc_type=doc_type,
        loss_date=loss_date or None,
        run_vision=True,
    )
    return json.dumps(rec, indent=2)


def _resolve_claim_image_to_path(
    claim_id: str = "", asset_id: str = ""
) -> tuple[str, str]:
    """Resolve a claim image from MongoDB/GridFS to a local temp file path."""
    from storage import mongo_assets as A

    if asset_id:
        return (
            A.resolve_to_tempfile(asset_id, kind=A.KIND_CLAIM_IMAGE),
            f"asset_id={asset_id}",
        )
    if claim_id:
        database = A._resolve_db()
        # primary: the processed per-claim case record (claim_cases)
        case = database[A.COLL_CLAIM_CASES].find_one({"case_id": claim_id}) or {}
        ref = case.get("image_ref")
        if ref:
            return (
                A.resolve_to_tempfile(ref, kind=A.KIND_CLAIM_IMAGE),
                f"case {claim_id} image_ref",
            )
        # legacy fallback: a raw claims doc carrying image_ref
        from agents.intake_agent.data_store import DEFAULT_COLLECTION

        legacy = database[DEFAULT_COLLECTION].find_one({"claim_id": claim_id}) or {}
        if legacy.get("image_ref"):
            return (
                A.resolve_to_tempfile(legacy["image_ref"], kind=A.KIND_CLAIM_IMAGE),
                f"claim {claim_id} image_ref",
            )
        meta = A.find_one_asset(kind=A.KIND_CLAIM_IMAGE, claim_id=claim_id)
        if meta:
            return (
                A.resolve_to_tempfile(meta["asset_id"], kind=A.KIND_CLAIM_IMAGE),
                f"claim {claim_id} asset",
            )
    raise FileNotFoundError(
        f"No image asset found (claim_id={claim_id!r}, asset_id={asset_id!r}). "
        "Seed assets with scripts/seed_assets.py."
    )


def analyze_document_from_mongo(
    claim_id: str = "",
    asset_id: str = "",
    doc_type: str = "damage_photo",
    loss_date: str | None = None,
) -> dict[str, Any]:
    """Fetch a claim image from MongoDB/GridFS and run the full forensic analysis.

    Downloads the asset to a temp file so the existing path-based layers work
    unchanged, then deletes it. This is the Mongo-only entry point for the live flow.
    """
    try:
        path, source = _resolve_claim_image_to_path(
            claim_id=claim_id, asset_id=asset_id
        )
    except Exception as exc:  # noqa: BLE001
        return {"claim_id": claim_id, "error": str(exc)}
    try:
        rec = analyze_document(
            path,
            claim_id=claim_id,
            doc_type=doc_type,
            loss_date=loss_date,
            run_vision=True,
        )
        rec["image_source"] = source
        return rec
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@tool
def analyze_claim_document_tool(
    claim_id: str = "",
    asset_id: str = "",
    doc_type: str = "damage_photo",
    loss_date: str = "",
) -> str:
    """Run the FULL authenticity analysis on a claim image stored in MongoDB (GridFS)."""
    rec = analyze_document_from_mongo(
        claim_id=claim_id,
        asset_id=asset_id,
        doc_type=doc_type,
        loss_date=loss_date or None,
    )
    return json.dumps(rec, indent=2)


@tool
def list_batch_image_claims_tool(batch_id: str = "") -> str:
    """List the claim_ids in a batch that carry an attached image — analyze these."""
    image_claims: list[str] = []
    source = ""
    bid = (batch_id or "").strip()
    if bid:
        try:
            from agents.intake_agent.data_store import read_stream_manifests

            manifests = read_stream_manifests(bid)
            for m in manifests:
                claim = (m.get("claims") or [{}])[0]
                if claim.get("has_attached_image") and claim.get("claim_id"):
                    image_claims.append(claim["claim_id"])
            if manifests:
                source = "mongo:claim_manifests"
        except (
            Exception
        ):  # noqa: BLE001  (Mongo down / no manifests -> CSV fallback below)
            image_claims = []
    if not image_claims:
        try:
            from agents.intake_agent.tools import parse_claims_csv

            manifest = parse_claims_csv()
            image_claims = [
                c["claim_id"]
                for c in manifest["claims"]
                if c.get("has_attached_image") and c.get("claim_id")
            ]
            source = "local_csv"
        except Exception as exc:  # noqa: BLE001
            return json.dumps(
                {"batch_id": bid, "error": f"could not list image claims: {exc}"}
            )
    image_claims = sorted(set(image_claims))
    return json.dumps(
        {
            "batch_id": bid,
            "image_claims": image_claims,
            "count": len(image_claims),
            "source": source,
        }
    )


def _image_claims_for_batch(batch_id: str) -> tuple[list[str], str]:
    """Return (image_claim_ids, source) for a batch: Mongo manifests first, CSV fallback."""
    image_claims: list[str] = []
    source = ""
    bid = (batch_id or "").strip()
    if bid:
        try:
            from agents.intake_agent.data_store import read_stream_manifests

            manifests = read_stream_manifests(bid)
            for m in manifests:
                c = (m.get("claims") or [{}])[0]
                if c.get("has_attached_image") and c.get("claim_id"):
                    image_claims.append(c["claim_id"])
            if manifests:
                source = "mongo:claim_manifests"
        except Exception:  # noqa: BLE001
            image_claims = []
    if not image_claims:
        try:
            from agents.intake_agent.tools import parse_claims_csv

            manifest = parse_claims_csv()
            image_claims = [
                c["claim_id"]
                for c in manifest["claims"]
                if c.get("has_attached_image") and c.get("claim_id")
            ]
            source = "local_csv"
        except Exception:  # noqa: BLE001
            pass
    return sorted(set(image_claims)), source


@tool
def analyze_batch_documents_tool(batch_id: str = "") -> str:
    """Analyze EVERY image claim in a batch, PERSIST the doc_authenticity JSON, return a summary."""
    claims, source = _image_claims_for_batch(batch_id)
    if not claims:
        return json.dumps(
            {
                "batch_id": batch_id,
                "analyzed": 0,
                "note": "no claims with attached images for this batch",
            }
        )
    documents = [analyze_document_from_mongo(claim_id=cid) for cid in claims]
    verdict_line = ", ".join(
        f"{d.get('claim_id')}:{d.get('verdict')}" for d in documents
    )
    payload = {
        "message_type": "doc_authenticity",
        "batch_id": batch_id,
        "documents_analyzed": documents,
        "summary": f"{len(documents)} analyzed ({verdict_line})",
    }
    summary = {
        "message_type": "doc_authenticity",
        "batch_id": batch_id,
        "analyzed": len(documents),
        "image_source": source,
        "by_claim": [
            {
                "claim_id": d.get("claim_id"),
                "verdict": d.get("verdict"),
                "authenticity_score": d.get("authenticity_score"),
                "recommend_review": d.get("recommend_review"),
            }
            for d in documents
        ],
        "summary": payload["summary"],
    }
    try:
        from storage.findings_store import write_findings

        write_findings(
            batch_id,
            "doc_authenticity",
            payload,
            summary=payload["summary"],
            agent="doc_authenticity_agent",
        )
        summary["persisted"] = True
        summary["note"] = (
            "Full doc_authenticity JSON is in the shared store — do NOT paste it; "
            "the Fraud and Decision agents read it via read_findings_tool."
        )
    except Exception as exc:  # noqa: BLE001
        summary["persisted"] = False
        summary["persist_error"] = str(exc)
        summary["payload"] = payload  # fallback so the data is not lost
    return json.dumps(summary, indent=2)


DOC_AUTH_TOOLS = [
    analyze_batch_documents_tool,
    list_batch_image_claims_tool,
    analyze_claim_document_tool,
    analyze_document_tool,
    analyze_exif_tool,
    check_c2pa_tool,
    detect_compression_tool,
    analyze_vision_tool,
] + FINDINGS_READ_TOOLS
