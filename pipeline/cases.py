"""Per-claim CASE pipeline: build -> validate -> load."""
from __future__ import annotations

import json
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any

from agents.intake_agent.tools import (
    DATA_DIR, DEFAULT_CLAIMS_CSV, VALID_LOSS_TYPES, _normalize_claim,
)

CASE_SCHEMA_VERSION = "case-v1"
DEFAULT_CASES_DIR = DATA_DIR / "cases"

# Per-claim image files (only claims whose has_attached_image is TRUE). label is a
# neutral provenance tag for the asset, not a fraud judgement.
CLAIM_IMAGE_MAP = {
    "CLM-0007": ("sample_deepfake_image.jpg", "claim_photo"),
    "CLM-0008": ("sample_claim_image.jpg", "scanned_form"),
}
# Single doctored demo policy referenced by every case in this scope.
DEMO_POLICY_FILE = "sample_policy.pdf"

CLAIM_FIELDS = [
    "claim_id", "claimant_id", "policy_number", "policy_inception_date",
    "loss_date", "loss_type", "claimed_amount", "state", "provider",
    "tow_company", "has_attached_image",
]


# ---------------------------------------------------------------------------
# Build (pure)
# ---------------------------------------------------------------------------
def _parse_date(value: str | None) -> date | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _derived(claim: dict[str, Any]) -> dict[str, Any]:
    """Neutral intake-level derived facts (objective, not fraud judgements)."""
    inc = _parse_date(claim.get("policy_inception_date"))
    loss = _parse_date(claim.get("loss_date"))
    age = (loss - inc).days if (inc and loss) else None
    return {
        "policy_age_days": age,
        "policy_new_at_loss": (age is not None and 0 <= age < 30),
        "has_image": bool(claim.get("has_attached_image")),
    }


def build_case(claim_row: dict[str, Any], built_at: str | None = None) -> dict[str, Any]:
    """Build one processed case record from a raw claim row (CSV or dict)."""
    claim = _normalize_claim(claim_row)
    cid = claim["claim_id"]
    image_file, image_label = CLAIM_IMAGE_MAP.get(cid, (None, None))
    return {
        "case_id": cid,
        "schema_version": CASE_SCHEMA_VERSION,
        "built_at": built_at or datetime.now(timezone.utc).isoformat(),
        "claim": {k: claim.get(k) for k in CLAIM_FIELDS},
        "derived": _derived(claim),
        "assets": {
            "image_file": image_file,
            "image_label": image_label,
            "policy_file": DEMO_POLICY_FILE,
        },
    }


def build_all_cases(csv_path: str | Path | None = None,
                    built_at: str | None = None) -> list[dict[str, Any]]:
    """Build all cases from the claims CSV, ordered by case_id."""
    import csv as _csv
    path = Path(csv_path) if csv_path else DEFAULT_CLAIMS_CSV
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = [r for r in _csv.DictReader(fh) if any((v or "").strip() for v in r.values())]
    cases = [build_case(r, built_at=built_at) for r in rows]
    cases.sort(key=lambda c: c["case_id"] or "")
    return cases


def write_cases_local(cases: list[dict[str, Any]],
                      out_dir: str | Path | None = None) -> Path:
    """Write one JSON file per case to a local directory. Returns the directory."""
    out = Path(out_dir) if out_dir else DEFAULT_CASES_DIR
    out.mkdir(parents=True, exist_ok=True)
    for c in cases:
        (out / f"{c['case_id']}.json").write_text(
            json.dumps(c, indent=2, sort_keys=True), encoding="utf-8")
    (out / "_index.json").write_text(
        json.dumps({"count": len(cases), "case_ids": [c["case_id"] for c in cases]},
                   indent=2), encoding="utf-8")
    return out


def read_cases_local(cases_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Read all case JSONs from a local directory (ignores _index.json)."""
    d = Path(cases_dir) if cases_dir else DEFAULT_CASES_DIR
    files = sorted(p for p in d.glob("*.json") if p.name != "_index.json")
    return [json.loads(p.read_text(encoding="utf-8")) for p in files]


# ---------------------------------------------------------------------------
# Validate (pure-ish: checks local file existence under data_dir)
# ---------------------------------------------------------------------------
def validate_case(case: dict[str, Any], data_dir: str | Path | None = None) -> list[str]:
    """Return a list of validation error strings ([] means the case is valid)."""
    dd = Path(data_dir) if data_dir else DATA_DIR
    errs: list[str] = []

    if case.get("schema_version") != CASE_SCHEMA_VERSION:
        errs.append(f"schema_version != {CASE_SCHEMA_VERSION}")
    claim = case.get("claim") or {}
    cid = case.get("case_id")
    if not cid:
        errs.append("missing case_id")
    if cid != claim.get("claim_id"):
        errs.append("case_id != claim.claim_id")

    for f in CLAIM_FIELDS:
        if f not in claim:
            errs.append(f"claim missing field: {f}")
    # required non-null
    for f in ["claim_id", "claimant_id", "policy_number", "policy_inception_date",
              "loss_date", "loss_type", "claimed_amount", "state"]:
        if claim.get(f) in (None, ""):
            errs.append(f"claim.{f} is required")

    lt = claim.get("loss_type")
    if lt is not None and lt not in VALID_LOSS_TYPES:
        errs.append(f"invalid loss_type: {lt}")
    st = claim.get("state")
    if st is not None and (len(str(st)) != 2 or not str(st).isupper()):
        errs.append(f"state not a 2-letter upper code: {st}")
    amt = claim.get("claimed_amount")
    if amt is not None and not (isinstance(amt, (int, float)) and amt > 0):
        errs.append(f"claimed_amount must be a positive number: {amt}")

    inc = _parse_date(claim.get("policy_inception_date"))
    loss = _parse_date(claim.get("loss_date"))
    if claim.get("policy_inception_date") and inc is None:
        errs.append("policy_inception_date not ISO YYYY-MM-DD")
    if claim.get("loss_date") and loss is None:
        errs.append("loss_date not ISO YYYY-MM-DD")
    if inc and loss and loss < inc:
        errs.append("loss_date is before policy_inception_date")

    derived = case.get("derived") or {}
    if inc and loss and derived.get("policy_age_days") != (loss - inc).days:
        errs.append("derived.policy_age_days inconsistent with dates")

    assets = case.get("assets") or {}
    has_img = bool(claim.get("has_attached_image"))
    img = assets.get("image_file")
    if has_img and not img:
        errs.append("has_attached_image is true but assets.image_file is missing")
    if img and not (dd / img).exists():
        errs.append(f"image_file not found on disk: {img}")
    pol = assets.get("policy_file")
    if not pol:
        errs.append("assets.policy_file is missing")
    elif not (dd / pol).exists():
        errs.append(f"policy_file not found on disk: {pol}")

    return errs


def validate_cases(cases: list[dict[str, Any]],
                   data_dir: str | Path | None = None) -> dict[str, Any]:
    """Validate a list of cases. Returns a report dict with per-case errors."""
    seen: set[str] = set()
    results = {}
    ok = 0
    for c in cases:
        errs = validate_case(c, data_dir)
        cid = c.get("case_id") or "<no id>"
        if cid in seen:
            errs = errs + [f"duplicate case_id: {cid}"]
        seen.add(cid)
        results[cid] = errs
        if not errs:
            ok += 1
    return {"total": len(cases), "valid": ok, "invalid": len(cases) - ok, "errors": results}


# ---------------------------------------------------------------------------
# Load (DB) — only per-claim cases + their own binaries
# ---------------------------------------------------------------------------
def load_cases_to_mongo(cases: list[dict[str, Any]],
                        data_dir: str | Path | None = None,
                        *, db: Any = None, uri: str | None = None,
                        db_name: str | None = None, drop: bool = False) -> dict[str, Any]:
    """Upsert validated cases into `claim_cases`; upload each case's image + policy to
    GridFS and set image_ref / policy_ref. Refuses to load if any case is invalid."""
    from storage import mongo_assets as A

    dd = Path(data_dir) if data_dir else DATA_DIR
    report = validate_cases(cases, dd)
    if report["invalid"]:
        raise ValueError(f"{report['invalid']} invalid case(s); fix before loading: "
                         f"{[k for k, v in report['errors'].items() if v]}")

    database = A._resolve_db(db, uri, db_name)
    col = database[A.COLL_CLAIM_CASES]
    if drop:
        col.delete_many({})
        for c in (f"{A.ASSET_BUCKET}.files", f"{A.ASSET_BUCKET}.chunks"):
            database[c].delete_many({})

    policy_ref_cache: dict[str, str] = {}
    loaded = 0
    for case in cases:
        cid = case["case_id"]
        assets = case.get("assets") or {}
        doc = dict(case)

        img = assets.get("image_file")
        if img:
            doc["image_ref"] = A.put_asset(
                dd / img, kind=A.KIND_CLAIM_IMAGE, claim_id=cid,
                label=assets.get("image_label"), db=database)
        pol = assets.get("policy_file")
        if pol:
            if pol not in policy_ref_cache:
                policy_ref_cache[pol] = A.put_asset(
                    dd / pol, kind=A.KIND_POLICY_PDF, label="doctored_demo", db=database)
            doc["policy_ref"] = policy_ref_cache[pol]

        col.replace_one({"case_id": cid}, doc, upsert=True)
        loaded += 1

    return {
        "loaded": loaded,
        "collection": A.COLL_CLAIM_CASES,
        "policy_assets": len(policy_ref_cache),
        "image_assets": sum(1 for c in cases if (c.get("assets") or {}).get("image_file")),
        "total_in_collection": col.count_documents({}),
    }
