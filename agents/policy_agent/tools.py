"""Policy Risk Analyzer Agent Tools (Agent 5)."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from storage.findings_store import write_findings

logger = logging.getLogger("nexusmesh.policy")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "data"
SAMPLE_POLICY_PDF = DATA_DIR / "sample_policy.pdf"
ISO_POLICY_PDF = DATA_DIR / "sample_policy_iso_original.pdf"

FL_MINIMUMS = {
    "bi_per_person": 10_000,
    "bi_per_occurrence": 20_000,
    "pd": 10_000,
    "pip": 10_000,
}

MOCK_RBC_DB: dict[str, float] = {
    "SUNSHINE-AUTO": 185.0,
    "COASTAL-INS": 312.0,
    "GATEWAY-MUTUAL": 254.0,
    "DEFAULT": 225.0,
}


@tool
def extract_policy_clauses_tool(pdf_path: str = "") -> str:
    """Extract and structure clauses from a policy PDF."""
    path = Path(pdf_path) if pdf_path else SAMPLE_POLICY_PDF
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.exists():
        path = SAMPLE_POLICY_PDF  # fallback to demo file

    raw_text = ""
    source = "unknown"

    try:
        import fitz  # PyMuPDF

        doc = fitz.open(str(path))
        pages = [page.get_text() for page in doc]
        raw_text = "\n".join(pages)
        source = "pymupdf"
        logger.info("Extracted %d chars from %s via PyMuPDF", len(raw_text), path.name)
    except Exception as exc:
        logger.debug("PyMuPDF failed: %s", exc)

    # ── Try pdfminer ──────────────────────────────────────────────────────────
    if not raw_text:
        try:
            from pdfminer.high_level import extract_text as pdfminer_extract

            raw_text = pdfminer_extract(str(path))
            source = "pdfminer"
            logger.info(
                "Extracted %d chars from %s via pdfminer", len(raw_text), path.name
            )
        except Exception as exc:
            logger.debug("pdfminer failed: %s", exc)

    # ── Stub fallback — known demo policy content ─────────────────────────────
    if not raw_text or len(raw_text) < 50:
        logger.warning("PDF extraction failed or empty — using known demo policy stub")
        raw_text = (
            "POLICY NUMBER: POL-FL-2026-001\n"
            "INSURED: SUNSHINE-AUTO CARRIERS INC\n"
            "POLICY PERIOD: 2026-01-01 to 2026-12-31\n"
            "STATE: FL\n"
            "COVERAGE A - BODILY INJURY LIABILITY: $10,000 per person / $20,000 per occurrence\n"
            "COVERAGE B - PROPERTY DAMAGE LIABILITY: $10,000\n"
            "COVERAGE C - PERSONAL INJURY PROTECTION (PIP): NOT INCLUDED\n"
            "COVERAGE D - COLLISION: $500 deductible\n"
            "COVERAGE E - COMPREHENSIVE: $250 deductible\n"
            "UM/UIM COVERAGE: NOT INCLUDED\n"
            "MEDPAY: NOT INCLUDED\n"
            "EXCLUSIONS: Racing, intentional acts\n"
            "NOTICE PROVISIONS: Report within 30 days\n"
            "CARRIER RBC RATIO: 185%\n"
        )
        source = "demo_stub"

    text_upper = raw_text.upper()

    def _extract_limit(pattern: str, text: str) -> int | None:
        """Pull a dollar amount near a regex keyword. Handles both 'KEYWORD...$X' and '$X...KEYWORD'."""
        m = re.search(pattern + r".*?\$\s*([\d,]+)", text, re.IGNORECASE | re.DOTALL)
        if m and m.group(1):
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                pass
        m2 = re.search(r"\$\s*([\d,]+)\s*" + pattern, text, re.IGNORECASE | re.DOTALL)
        if m2 and m2.group(1):
            try:
                return int(m2.group(1).replace(",", ""))
            except ValueError:
                pass
        return None

    has_umuim = bool(
        re.search(r"UM/UIM|UNINSURED|UNDERINSURED", raw_text, re.IGNORECASE)
    ) and not re.search(
        r"UM/UIM.*?NOT INCLUDED|NOT INCLUDED.*?UM/UIM",
        raw_text,
        re.IGNORECASE | re.DOTALL,
    )
    has_pip = bool(
        re.search(r"\bPIP\b|PERSONAL INJURY PROTECTION", raw_text, re.IGNORECASE)
    ) and not re.search(
        r"PIP.*?NOT INCLUDED|NOT INCLUDED.*?PIP", raw_text, re.IGNORECASE | re.DOTALL
    )
    has_medpay = bool(
        re.search(r"MEDPAY|MEDICAL PAYMENTS", raw_text, re.IGNORECASE)
    ) and not re.search(r"MEDPAY.*?NOT INCLUDED", raw_text, re.IGNORECASE | re.DOTALL)

    bi_pp = _extract_limit(r"per person", raw_text)
    bi_po = _extract_limit(r"per (?:occurrence|accident)", raw_text)
    pd = _extract_limit(r"PROPERTY DAMAGE", raw_text)
    pip_limit = (
        _extract_limit(r"(?:PERSONAL INJURY PROTECTION|PIP)", raw_text)
        if has_pip
        else None
    )

    rbc_match = re.search(r"RBC RATIO[:\s]+([\d.]+)%", raw_text, re.IGNORECASE)
    rbc_from_text = float(rbc_match.group(1)) if rbc_match else None

    carrier_match = re.search(r"INSURED:\s*(.+)", raw_text, re.IGNORECASE)
    carrier_id = carrier_match.group(1).strip()[:40] if carrier_match else "DEFAULT"
    carrier_key = re.sub(r"\s+", "-", carrier_id.upper())[:20]

    clauses = {
        "carrier_id": carrier_key,
        "state": "FL",  # primary state from demo policy
        "coverages": {
            "bi_per_person": bi_pp,
            "bi_per_occurrence": bi_po,
            "pd": pd,
            "pip": pip_limit,
            "has_umuim": has_umuim,
            "has_pip": has_pip,
            "has_medpay": has_medpay,
        },
        "language": {
            "has_exclusions": bool(re.search(r"EXCLUSION", raw_text, re.IGNORECASE)),
            "has_notice_provisions": bool(
                re.search(r"NOTICE|REPORT WITHIN", raw_text, re.IGNORECASE)
            ),
            "occurrence_vs_claims_made": (
                "occurrence"
                if re.search(r"OCCURRENCE", raw_text, re.IGNORECASE)
                else "unknown"
            ),
        },
        "financial": {
            "rbc_ratio_from_text": rbc_from_text,
        },
    }

    return json.dumps(
        {
            "clauses": clauses,
            "raw_text_length": len(raw_text),
            "source": source,
            "pdf_path": str(path),
        },
        indent=2,
    )


@tool
def check_fl_minimums_tool(
    bi_per_person: int,
    bi_per_occurrence: int,
    pd: int,
    pip: int,
    state: str = "FL",
) -> str:
    """Check BI/PD/PIP limits against Florida (or specified state) statutory minimums."""
    state = state.strip().upper()
    gaps = []

    if state == "FL":
        minimums = FL_MINIMUMS
        naic_ref = "FL FS 627.736 + NAIC Model Law 900"
    else:
        minimums = {
            "bi_per_person": 25_000,
            "bi_per_occurrence": 50_000,
            "pd": 25_000,
            "pip": 0,
        }
        naic_ref = "NAIC Model Law 900 (generic minimums)"

    checks = {
        "bi_per_person": {
            "actual": bi_per_person,
            "minimum": minimums["bi_per_person"],
        },
        "bi_per_occurrence": {
            "actual": bi_per_occurrence,
            "minimum": minimums["bi_per_occurrence"],
        },
        "pd": {"actual": pd, "minimum": minimums["pd"]},
        "pip": {"actual": pip, "minimum": minimums.get("pip", 0)},
    }

    results = {}
    for coverage, vals in checks.items():
        passes = vals["actual"] >= vals["minimum"] if vals["minimum"] > 0 else True
        severity = "OK" if passes else ("High" if vals["actual"] == 0 else "Medium")
        results[coverage] = {
            "actual": vals["actual"],
            "minimum": vals["minimum"],
            "passes": passes,
            "severity": severity,
        }
        if not passes:
            label = coverage.replace("_", " ").upper()
            gaps.append(
                {
                    "clause_type": coverage.split("_")[0].upper(),
                    "finding": f"{label}: ${vals['actual']:,} is below FL statutory minimum of ${vals['minimum']:,}",
                    "naic_reference": naic_ref,
                    "severity": severity,
                }
            )

    overall = (
        "OK"
        if not gaps
        else ("High" if any(g["severity"] == "High" for g in gaps) else "Medium")
    )

    return json.dumps(
        {
            "state": state,
            "checks": results,
            "gaps": gaps,
            "overall": overall,
            "naic_reference": naic_ref,
        },
        indent=2,
    )


@tool
def check_rbc_ratio_tool(carrier_id: str) -> str:
    """Check a carrier's Risk-Based Capital (RBC) ratio against the NAIC 200% action level."""
    carrier_id = carrier_id.strip().upper()

    # Partial-match lookup
    ratio = None
    for key, val in MOCK_RBC_DB.items():
        if key in carrier_id or carrier_id in key:
            ratio = val
            break
    if ratio is None:
        ratio = MOCK_RBC_DB["DEFAULT"]

    above_200 = ratio >= 200.0
    if ratio < 150.0:
        status = "CRITICAL"
    elif ratio < 200.0:
        status = "WARNING"
    else:
        status = "OK"

    return json.dumps(
        {
            "carrier_id": carrier_id,
            "rbc_ratio": ratio,
            "action_level_200pct": above_200,
            "status": status,
            "naic_reference": "NAIC RBC Requirements — Company Action Level 200% of ACL",
            "note": "Mock RBC database. In production, fetch from NAIC IRIS filing.",
        },
        indent=2,
    )


@tool
def identify_coverage_gaps_tool(batch_id: str, pdf_path: str = "") -> str:
    """Orchestrate full policy risk analysis: extract clauses, check minimums, check RBC."""
    batch_id = batch_id.strip()
    logger.info(
        "identify_coverage_gaps_tool: batch=%s pdf=%s", batch_id, pdf_path or "default"
    )

    clauses_raw = extract_policy_clauses_tool.invoke({"pdf_path": pdf_path})
    clauses_data = json.loads(clauses_raw)
    clauses = clauses_data.get("clauses", {})
    covs = clauses.get("coverages", {})
    lang = clauses.get("language", {})
    fin = clauses.get("financial", {})
    carrier_id = clauses.get("carrier_id", "DEFAULT")
    state = clauses.get("state", "FL")

    all_gaps: list[dict] = []
    language_flags: list[str] = []

    if not covs.get("has_umuim"):
        all_gaps.append(
            {
                "clause_type": "UM/UIM",
                "finding": "Uninsured/Underinsured Motorist coverage is absent from this policy. "
                "Florida recommends UM/UIM for adequate consumer protection.",
                "naic_reference": "NAIC Model Law 900 §4 + FL FS 627.727",
                "severity": "High",
            }
        )

    if not covs.get("has_pip"):
        all_gaps.append(
            {
                "clause_type": "PIP",
                "finding": "Personal Injury Protection (PIP) is absent. Florida mandates $10,000 "
                "minimum PIP for all registered vehicles (FS 627.736).",
                "naic_reference": "FL FS 627.736",
                "severity": "Critical",
            }
        )

    if not covs.get("has_medpay"):
        language_flags.append(
            "MedPay not included. Consider adding for states where PIP is unavailable."
        )

    bi_pp = covs.get("bi_per_person") or 0
    bi_po = covs.get("bi_per_occurrence") or 0
    pd_val = covs.get("pd") or 0
    pip_val = covs.get("pip") or 0

    if state == "FL":
        minimums_raw = check_fl_minimums_tool.invoke(
            {
                "bi_per_person": bi_pp,
                "bi_per_occurrence": bi_po,
                "pd": pd_val,
                "pip": pip_val,
                "state": "FL",
            }
        )
        minimums_data = json.loads(minimums_raw)
        all_gaps.extend(minimums_data.get("gaps", []))

    if not lang.get("has_exclusions"):
        language_flags.append(
            "Standard exclusion clauses not found — verify policy language."
        )
    if not lang.get("has_notice_provisions"):
        language_flags.append(
            "Notice provisions absent — prompt-reporting requirement may be missing."
        )
    if lang.get("occurrence_vs_claims_made") == "unknown":
        language_flags.append(
            "Policy form type (occurrence vs claims-made) not clearly stated."
        )

    rbc_raw = check_rbc_ratio_tool.invoke(carrier_id)
    rbc_data = json.loads(rbc_raw)
    if rbc_data["status"] in ("WARNING", "CRITICAL"):
        severity = "Critical" if rbc_data["status"] == "CRITICAL" else "High"
        all_gaps.append(
            {
                "clause_type": "Financial",
                "finding": (
                    f"Carrier {carrier_id} RBC ratio {rbc_data['rbc_ratio']:.1f}% is below "
                    f"NAIC 200% Company Action Level. Regulatory intervention may be triggered."
                ),
                "naic_reference": "NAIC RBC Requirements — Company Action Level",
                "severity": severity,
            }
        )

    severities = [g["severity"] for g in all_gaps]
    if "Critical" in severities:
        overall_risk = "Critical"
    elif severities.count("High") >= 3 or (
        severities.count("High") >= 1 and len(all_gaps) >= 3
    ):
        overall_risk = "High"
    elif len(all_gaps) >= 2:
        overall_risk = "High"
    elif len(all_gaps) == 1:
        overall_risk = "Medium"
    else:
        overall_risk = "Low"

    findings = {
        "message_type": "policy_risks",
        "batch_id": batch_id,
        "overall_risk": overall_risk,
        "coverage_gaps": all_gaps,
        "rbc_check": rbc_data,
        "language_flags": language_flags,
        "summary": (
            f"{len(all_gaps)} coverage gap(s) found. "
            f"Overall risk: {overall_risk}. "
            f"RBC: {rbc_data['rbc_ratio']:.1f}% ({rbc_data['status']})."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        write_findings(
            batch_id,
            "policy_risks",
            findings,
            summary=findings["summary"],
            agent="policy_agent",
        )
        logger.info(
            "Policy risks persisted for batch %s (%d gaps)", batch_id, len(all_gaps)
        )
    except Exception as exc:
        logger.warning("Failed to persist policy risks: %s", exc)

    return json.dumps(findings, indent=2)


POLICY_TOOLS = [
    extract_policy_clauses_tool,
    check_fl_minimums_tool,
    check_rbc_ratio_tool,
    identify_coverage_gaps_tool,
]
