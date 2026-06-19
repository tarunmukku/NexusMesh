"""Fraud Detection Agent tools (Agent 3)."""

from __future__ import annotations

import csv
import json
import os
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from langchain_core.tools import tool

from band.findings_tools import FINDINGS_READ_TOOLS

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "data"

FRAUD_DB_PATH = DATA_DIR / "mock_fraud_db.json"
RING_PATH = DATA_DIR / "mock_ring_relationships.json"
HISTORY_PATH = DATA_DIR / "mock_investigation_history.json"
OFAC_PATH = DATA_DIR / "ofac_sdn.csv"

# Traffic-light tiers (docs/16_HYBRID_CLAIM_FLOW.md).
TIER_GREEN, TIER_AMBER, TIER_RED = "GREEN", "AMBER", "RED"
RING_SHARED_ENTITY_THRESHOLD = 3  # Šubelj: claims sharing >= 3 entities seed a ring.
DOC_AUTH_FRAUD_THRESHOLD = 0.5  # authenticity_score < this adds +20.
DOC_AUTH_WEIGHT = 20


# ---------------------------------------------------------------------------
# Reference-data loaders (LOCAL files; cached)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def load_fraud_db(path: str | None = None) -> dict[str, Any]:
    """Load the mock fraud-pattern DB (patterns, flagged tow/provider lists, ring zips)."""
    p = Path(path) if path else FRAUD_DB_PATH
    return json.loads(p.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_ring_relationships(path: str | None = None) -> dict[str, Any]:
    """Load the shared-entity graph used for ring detection."""
    p = Path(path) if path else RING_PATH
    return json.loads(p.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_investigation_history(path: str | None = None) -> list[dict[str, Any]]:
    """Load seed SIU investigation outcomes (feedback-loop precedents)."""
    p = Path(path) if path else HISTORY_PATH
    return json.loads(p.read_text(encoding="utf-8")).get("outcomes", [])


@lru_cache(maxsize=1)
def load_ofac_names(path: str | None = None) -> tuple[str, ...]:
    """Load uppercased entity names from the real OFAC SDN CSV (column 2)."""
    p = Path(path) if path else OFAC_PATH
    names: list[str] = []
    with p.open(newline="", encoding="utf-8-sig", errors="ignore") as fh:
        for row in csv.reader(fh):
            if len(row) >= 2 and row[1].strip():
                names.append(row[1].strip().upper())
    return tuple(names)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def _pattern_index(fraud_db: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for p in fraud_db.get("individual_patterns", []) + fraud_db.get(
        "network_patterns", []
    ):
        out[p["id"]] = p
    return out


def _ofac_provider_hit(
    provider: str | None, fraud_db: dict[str, Any], ofac_names: Iterable[str]
) -> bool:
    """True if the provider matches the OFAC SDN list or the mock flagged-provider list."""
    if not provider:
        return False
    name = provider.strip().upper()
    if name in {n.upper() for n in fraud_db.get("flagged_provider_ids", [])}:
        return True
    return name in set(ofac_names)


def match_individual_patterns(
    claim: dict[str, Any],
    batch_claims: list[dict[str, Any]] | None,
    fraud_db: dict[str, Any],
    ofac_names: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Return the individual fraud patterns a single claim matches.

    Each match is {id, name, weight, detail}. `batch_claims` is needed for the
    duplicate-claimant pattern (P004); pass the full batch.
    """
    patterns = _pattern_index(fraud_db)
    ofac_names = ofac_names if ofac_names is not None else load_ofac_names()
    flagged_tow = {t.upper() for t in fraud_db.get("flagged_tow_companies", [])}
    matched: list[dict[str, Any]] = []

    def add(pid: str, detail: str) -> None:
        p = patterns.get(pid, {})
        matched.append(
            {
                "id": pid,
                "name": p.get("name", pid),
                "weight": int(p.get("weight", 0)),
                "detail": detail,
            }
        )

    # P001 — early claim (<30 days post-inception)
    inc = _parse_date(claim.get("policy_inception_date"))
    loss = _parse_date(claim.get("loss_date"))
    if inc and loss:
        days = (loss - inc).days
        if 0 <= days < 30:
            add("P001", f"loss filed {days} days after policy inception")

    # P002 — round amount (multiple of $500 and > $5,000)
    amt = claim.get("claimed_amount")
    if isinstance(amt, (int, float)) and amt > 5000 and (round(amt) % 500 == 0):
        add("P002", f"claimed amount ${amt:,.0f} is a round multiple of $500")

    # P003 — OFAC / OIG-flagged provider
    if _ofac_provider_hit(claim.get("provider"), fraud_db, ofac_names):
        add(
            "P003",
            f"provider '{claim.get('provider')}' matches a federal exclusion/sanctions list",
        )

    # P004 — duplicate claimant in the batch
    if batch_claims:
        cid = claim.get("claimant_id")
        if cid:
            dupes = [
                c.get("claim_id")
                for c in batch_claims
                if c.get("claimant_id") == cid
                and c.get("claim_id") != claim.get("claim_id")
            ]
            if dupes:
                add(
                    "P004",
                    f"claimant {cid} also appears on {', '.join(sorted(d for d in dupes if d))}",
                )

    # P005 — FL PIP staged crash (FL + flagged tow company)
    state = (claim.get("state") or "").upper()
    tow = claim.get("tow_company") or ""
    if state == "FL" and tow and tow.upper() in flagged_tow:
        add("P005", f"FL claim using flagged tow company '{tow}'")

    return matched


# ---------------------------------------------------------------------------
# 2. Network intelligence — Šubelj shared-entity ring detection
# ---------------------------------------------------------------------------
def _claim_entity_map(
    relationships: dict[str, Any], claim_to_claimant: dict[str, str] | None = None
) -> dict[str, set[str]]:
    """Build claim_id -> set of namespaced entities ('tow:X', 'repair:Y', ...).

    Pulls every ``claim_to_*`` mapping in the relationships file. When a
    claim->claimant map is supplied, also folds in claimant-level phone/address
    entities so identity-sharing rings are visible.
    """
    entities: dict[str, set[str]] = {}
    for key, mapping in relationships.items():
        if not key.startswith("claim_to_") or not isinstance(mapping, dict):
            continue
        etype = key[len("claim_to_") :]
        for cid, val in mapping.items():
            if val:
                entities.setdefault(cid, set()).add(f"{etype}:{val}")

    if claim_to_claimant:
        phones = relationships.get("claimant_to_phone", {})
        addrs = relationships.get("claimant_to_address", {})
        for cid in list(entities):
            claimant = claim_to_claimant.get(cid)
            if not claimant:
                continue
            if phones.get(claimant):
                entities[cid].add(f"phone:{phones[claimant]}")
            if addrs.get(claimant):
                entities[cid].add(f"address:{addrs[claimant]}")
    return entities


def detect_fraud_rings(
    relationships: dict[str, Any],
    threshold: int = RING_SHARED_ENTITY_THRESHOLD,
    claim_to_claimant: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Šubelj (2011) shared-entity clustering.

    A *seed* ring is any pair of claims sharing >= `threshold` distinct entities.
    The ring is then expanded to its connected component: any claim sharing >= 1
    entity with a current member joins. Returns a list of rings, each:
        {ring_id, members[], shared_entities[], shared_tow[], shared_repair_medical[]}
    """
    entities = _claim_entity_map(relationships, claim_to_claimant)
    claims = sorted(entities)

    seeds: list[tuple[str, str]] = []
    for i, a in enumerate(claims):
        for b in claims[i + 1 :]:
            if len(entities[a] & entities[b]) >= threshold:
                seeds.append((a, b))
    if not seeds:
        return []

    parent: dict[str, str] = {c: c for c in claims}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        parent[find(x)] = find(y)

    seeded: set[str] = set()
    for a, b in seeds:
        union(a, b)
        seeded.update((a, b))

    components: dict[str, set[str]] = {}
    for c in seeded:
        components.setdefault(find(c), set()).add(c)

    rings: list[dict[str, Any]] = []
    for members in components.values():
        members = set(members)
        changed = True
        while changed:
            changed = False
            member_entities: set[str] = set().union(*(entities[m] for m in members))
            for c in claims:
                if c in members:
                    continue
                if entities[c] & member_entities:
                    members.add(c)
                    changed = True
        rings.append(
            {"members": sorted(members), "_entities": {m: entities[m] for m in members}}
        )

    rings.sort(key=lambda r: r["members"][0])
    for idx, ring in enumerate(rings, start=1):
        members = ring["members"]
        ents = ring.pop("_entities")
        counts: dict[str, int] = {}
        for m in members:
            for e in ents[m]:
                counts[e] = counts.get(e, 0) + 1
        shared = sorted(e for e, n in counts.items() if n >= 2)
        ring["ring_id"] = f"RING-{idx:03d}"
        ring["shared_entities"] = shared
        ring["shared_tow"] = sorted(
            e.split(":", 1)[1] for e in shared if e.startswith("tow_company:")
        )
        ring["shared_repair"] = sorted(
            e.split(":", 1)[1] for e in shared if e.startswith("repair_shop:")
        )
        ring["shared_medical"] = sorted(
            e.split(":", 1)[1] for e in shared if e.startswith("medical_provider:")
        )
        ring["_member_entities"] = ents  # kept for per-claim network scoring
    return rings


def network_patterns_for_claim(
    claim_id: str, rings: list[dict[str, Any]], fraud_db: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    patterns = _pattern_index(fraud_db)
    matched: list[dict[str, Any]] = []
    network = {
        "in_ring": False,
        "ring_id": None,
        "ring_members": [],
        "shared_entities": [],
    }

    for ring in rings:
        if claim_id not in ring["members"]:
            continue
        network = {
            "in_ring": True,
            "ring_id": ring["ring_id"],
            "ring_members": ring["members"],
            "shared_entities": ring["shared_entities"],
        }
        member_entities = ring.get("_member_entities", {})
        mine = member_entities.get(claim_id, set())
        others: set[str] = set()
        for m, ents in member_entities.items():
            if m != claim_id:
                others |= ents

        def add(pid: str, detail: str) -> None:
            p = patterns.get(pid, {})
            matched.append(
                {
                    "id": pid,
                    "name": p.get("name", pid),
                    "weight": int(p.get("weight", 0)),
                    "detail": detail,
                }
            )

        shares_tow = any(e.startswith("tow_company:") and e in others for e in mine)
        shares_repair = any(e.startswith("repair_shop:") and e in others for e in mine)
        shares_medical = any(
            e.startswith("medical_provider:") and e in others for e in mine
        )
        if shares_tow:
            tow = next(
                e.split(":", 1)[1]
                for e in mine
                if e.startswith("tow_company:") and e in others
            )
            add("N001", f"shares tow company '{tow}' with ring {ring['ring_id']}")
        if shares_repair and shares_medical:
            add(
                "N002",
                f"shares both repair shop and medical provider with ring {ring['ring_id']}",
            )
        break
    return matched, network


def history_precedents(
    matched_pattern_ids: Iterable[str], history: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Find past SIU outcomes whose patterns overlap this claim's patterns."""
    history = history if history is not None else load_investigation_history()
    mine = set(matched_pattern_ids)
    out: list[dict[str, Any]] = []
    for rec in history:
        overlap = mine & set(rec.get("patterns_involved", []))
        if not overlap:
            continue
        decision = rec.get("officer_decision", "")
        direction = (
            "confirmed_fraud"
            if decision == "APPROVED_FOR_INVESTIGATION"
            else (
                "false_positive"
                if decision == "REJECTED_FALSE_POSITIVE"
                else "prior_review"
            )
        )
        out.append(
            {
                "claim_id": rec.get("claim_id"),
                "batch_id": rec.get("batch_id"),
                "shared_patterns": sorted(overlap),
                "direction": direction,
                "officer_reason": rec.get("officer_reason"),
            }
        )
    return out


def compute_fraud_score(
    matched_patterns: list[dict[str, Any]], doc_authenticity_score: float | None = None
) -> int:
    """Weighted-sum of all matched pattern weights, plus the doc-authenticity boost,
    capped at 100."""
    total = sum(int(p.get("weight", 0)) for p in matched_patterns)
    if (
        doc_authenticity_score is not None
        and doc_authenticity_score < DOC_AUTH_FRAUD_THRESHOLD
    ):
        total += DOC_AUTH_WEIGHT
    return max(0, min(100, total))


def assign_tier(score: int) -> str:
    """🟢 GREEN 0-29 │ 🟡 AMBER 30-74 │ 🔴 RED 75-100."""
    if score >= 75:
        return TIER_RED
    if score >= 30:
        return TIER_AMBER
    return TIER_GREEN


def _reason_codes(
    matched: list[dict[str, Any]],
    network: dict[str, Any],
    precedents: list[dict[str, Any]],
    doc_score: float | None,
) -> list[str]:
    codes = [f"{p['name']} (+{p['weight']}): {p['detail']}" for p in matched]
    if doc_score is not None and doc_score < DOC_AUTH_FRAUD_THRESHOLD:
        codes.append(
            f"Manipulated/AI media (+{DOC_AUTH_WEIGHT}): authenticity score {doc_score:.2f} below {DOC_AUTH_FRAUD_THRESHOLD}"
        )
    for pr in precedents:
        verb = (
            "previously CONFIRMED fraud"
            if pr["direction"] == "confirmed_fraud"
            else (
                "previously REJECTED as a false positive"
                if pr["direction"] == "false_positive"
                else "previously reviewed"
            )
        )
        codes.append(
            f"Precedent: pattern(s) {', '.join(pr['shared_patterns'])} {verb} on "
            f"{pr['claim_id']} ({pr.get('batch_id')}) — {pr.get('officer_reason')}"
        )
    return codes


def score_claim(
    claim: dict[str, Any],
    batch_claims: list[dict[str, Any]],
    rings: list[dict[str, Any]],
    fraud_db: dict[str, Any],
    history: list[dict[str, Any]],
    ofac_names: Iterable[str] | None = None,
    doc_authenticity_score: float | None = None,
) -> dict[str, Any]:
    """Full per-claim fraud record (individual + network + history + score + tier)."""
    claim_id = claim.get("claim_id")
    individual = match_individual_patterns(claim, batch_claims, fraud_db, ofac_names)
    net_patterns, network = network_patterns_for_claim(claim_id, rings, fraud_db)
    matched = individual + net_patterns
    precedents = history_precedents([p["id"] for p in matched], history)
    score = compute_fraud_score(matched, doc_authenticity_score)
    tier = assign_tier(score)
    return {
        "claim_id": claim_id,
        "fraud_score": score,
        "tier": tier,
        "patterns_matched": [p["id"] for p in matched],
        "network_findings": network,
        "history_precedents": [
            f"{pr['claim_id']} ({pr['direction']}, patterns {','.join(pr['shared_patterns'])})"
            for pr in precedents
        ],
        "reason_codes": _reason_codes(
            matched, network, precedents, doc_authenticity_score
        ),
    }


def score_batch(
    claims: list[dict[str, Any]],
    relationships: dict[str, Any] | None = None,
    fraud_db: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    ofac_names: Iterable[str] | None = None,
    doc_authenticity: dict[str, float] | None = None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Score a whole batch and build the `fraud_findings` payload.

    `doc_authenticity` is an optional {claim_id: authenticity_score} map from the
    Document Authenticity Agent — a score < 0.5 adds +20 to that claim.
    """
    fraud_db = fraud_db if fraud_db is not None else load_fraud_db()
    relationships = (
        relationships if relationships is not None else load_ring_relationships()
    )
    history = history if history is not None else load_investigation_history()
    ofac_names = ofac_names if ofac_names is not None else load_ofac_names()
    doc_authenticity = doc_authenticity or {}

    claim_to_claimant = {
        c["claim_id"]: c.get("claimant_id") for c in claims if c.get("claim_id")
    }
    rings = detect_fraud_rings(relationships, claim_to_claimant=claim_to_claimant)

    flagged, clean = [], []
    total_exposure = 0.0
    for claim in claims:
        rec = score_claim(
            claim,
            claims,
            rings,
            fraud_db,
            history,
            ofac_names,
            doc_authenticity.get(claim.get("claim_id")),
        )
        if rec["tier"] == TIER_GREEN:
            clean.append(rec["claim_id"])
        else:
            flagged.append(rec)
            amt = claim.get("claimed_amount") or 0
            if isinstance(amt, (int, float)):
                total_exposure += amt

    precedent_count = sum(len(f["history_precedents"]) for f in flagged)
    rings_public = [
        {
            "ring_id": r["ring_id"],
            "members": r["members"],
            "shared_entities": r["shared_entities"],
        }
        for r in rings
    ]
    summary = (
        f"{len(flagged)}/{len(claims)} flagged incl. {len(rings_public)} ring(s). "
        f"Exposure: ${total_exposure:,.0f}. {precedent_count} history precedent(s) applied."
    )
    return {
        "message_type": "fraud_findings",
        "batch_id": batch_id,
        "flagged_claims": sorted(
            flagged, key=lambda r: (-r["fraud_score"], r["claim_id"])
        ),
        "rings_detected": rings_public,
        "clean_claims": sorted(clean),
        "summary": summary,
    }


@tool
def query_fraud_db_tool(
    claimant_id: str = "", provider: str = "", tow_company: str = "", state: str = ""
) -> str:
    """Check a claim's parties against the local fraud-pattern database."""
    fraud_db = load_fraud_db()
    flagged_tow = {t.upper() for t in fraud_db.get("flagged_tow_companies", [])}
    flagged_claimants = {c.upper() for c in fraud_db.get("flagged_claimant_ids", [])}
    hits = []
    if provider and _ofac_provider_hit(provider, fraud_db, load_ofac_names()):
        hits.append(
            {
                "id": "P003",
                "weight": 40,
                "detail": f"provider '{provider}' on exclusion/sanctions list",
            }
        )
    if tow_company and tow_company.upper() in flagged_tow:
        hits.append(
            {
                "id": "P005",
                "weight": 35,
                "detail": f"tow company '{tow_company}' flagged"
                + (" (FL staged-crash pattern)" if state.upper() == "FL" else ""),
            }
        )
    if claimant_id and claimant_id.upper() in flagged_claimants:
        hits.append(
            {
                "id": "flagged_claimant",
                "weight": 0,
                "detail": f"claimant {claimant_id} on watch list",
            }
        )
    return json.dumps({"hits": hits, "matched": bool(hits)}, indent=2)


@tool
def check_ofac_screen_tool(name: str) -> str:
    """Screen a name (provider/claimant/entity) against the real OFAC SDN list."""
    names = load_ofac_names()
    hit = name.strip().upper() in set(names) if name else False
    return json.dumps(
        {
            "name": name,
            "ofac_hit": hit,
            "list": "OFAC SDN (Specially Designated Nationals)",
        }
    )


@tool
def detect_fraud_rings_tool() -> str:
    """Run Šubelj (2011) shared-entity ring detection over the relationship graph."""
    rings = detect_fraud_rings(load_ring_relationships())
    public = [
        {
            "ring_id": r["ring_id"],
            "members": r["members"],
            "shared_entities": r["shared_entities"],
        }
        for r in rings
    ]
    return json.dumps({"rings": public}, indent=2)


@tool
def read_investigation_history_tool() -> str:
    """Load past SIU investigation outcomes for the feedback loop (v3)."""
    outcomes = list(load_investigation_history())
    try:
        from storage import mongo_assets as A

        db = A._resolve_db()
        for d in db[A.COLL_INVESTIGATION].find({}, {"_id": 0}):
            outcomes.append(d)
    except Exception:  # noqa: BLE001
        pass
    return json.dumps({"outcomes": outcomes, "count": len(outcomes)}, indent=2)


@tool
def compute_fraud_score_tool(
    patterns_json: str, doc_authenticity_score: float = -1.0
) -> str:
    """Compute a 0-100 fraud score + traffic-light tier from matched patterns."""
    try:
        patterns = json.loads(patterns_json) if patterns_json else []
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"invalid patterns_json: {exc}"})
    doc = doc_authenticity_score if doc_authenticity_score >= 0 else None
    score = compute_fraud_score(patterns, doc)
    return json.dumps({"fraud_score": score, "tier": assign_tier(score)})


def _fraud_summary(findings: dict) -> dict:
    """Compact, room-friendly summary of a fraud_findings payload."""
    flagged = findings.get("flagged_claims", [])
    by_tier: dict[str, list] = {}
    for f in flagged:
        by_tier.setdefault(f.get("tier"), []).append(f.get("claim_id"))
    return {
        "message_type": "fraud_findings",
        "batch_id": findings.get("batch_id"),
        "flagged_count": len(flagged),
        "red": sorted(by_tier.get("RED", [])),
        "amber": sorted(by_tier.get("AMBER", [])),
        "rings": [
            {"ring_id": r.get("ring_id"), "members": r.get("members")}
            for r in findings.get("rings_detected", [])
        ],
        "clean_count": len(findings.get("clean_claims", [])),
        "summary": findings.get("summary"),
    }


def _persist_fraud_findings(findings: dict) -> dict:
    """Write the FULL findings to the shared store and return a compact summary the agent
    posts to the room. If the store write fails (e.g. Mongo down), fall back to returning
    the full payload so nothing is lost."""
    summary = _fraud_summary(findings)
    try:
        from storage.findings_store import write_findings

        write_findings(
            findings.get("batch_id"),
            "fraud_findings",
            findings,
            summary=summary.get("summary"),
            agent="fraud_agent",
        )
        summary["persisted"] = True
        summary["persisted_to"] = "shared store (agent_findings)"
        summary["note"] = (
            "Full fraud_findings JSON is in the shared store — do NOT paste it "
            "into the room; downstream agents read it via read_findings_tool."
        )
    except Exception as exc:  # noqa: BLE001
        summary["persisted"] = False
        summary["persist_error"] = str(exc)
        summary["payload"] = findings
    return summary


def _doc_authenticity_scores(batch_id: str) -> dict:
    """Best-effort: pull doc_authenticity from the shared store and build a
    {claim_id: authenticity_score} map so a manipulated image (<0.5) adds +20."""
    try:
        from storage.findings_store import read_findings

        da = read_findings(batch_id, "doc_authenticity")
        if not da:
            return {}
        out = {}
        for d in da.get("documents_analyzed", []):
            cid, sc = d.get("claim_id"), d.get("authenticity_score")
            if cid and isinstance(sc, (int, float)):
                out[cid] = sc
        return out
    except Exception:  # noqa: BLE001
        return {}


@tool
def score_batch_tool(batch_id: str) -> str:
    """Score every claim in a persisted batch; PERSIST the findings, return a summary."""
    try:
        from agents.intake_agent.data_store import read_stream_manifests

        manifests = read_stream_manifests(batch_id.strip())
        if not manifests:
            return json.dumps(
                {
                    "error": f"No manifests for batch_id {batch_id}. "
                    "Has the Intake Agent persisted it?"
                }
            )
        claims = [(m.get("claims") or [{}])[0] for m in manifests]
        doc_auth = _doc_authenticity_scores(batch_id.strip())
        findings = score_batch(
            claims, doc_authenticity=doc_auth or None, batch_id=batch_id.strip()
        )
        return json.dumps(_persist_fraud_findings(findings), indent=2)
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "error": f"Mongo batch scoring failed: {exc}",
                "fallback": "Call score_local_batch_tool to read data/sample_claims.csv.",
            }
        )


@tool
def score_local_batch_tool(csv_path: str = "") -> str:
    """Score the local CSV claims batch (offline fallback for score_batch_tool).

    Reads data/sample_claims.csv (or csv_path), runs the full fraud pipeline, PERSISTS the
    findings to the shared store, and returns the compact summary. Use only when MongoDB
    is unavailable for reading the batch.
    """
    from agents.intake_agent.tools import parse_claims_csv

    manifest = parse_claims_csv(csv_path or None)
    findings = score_batch(manifest["claims"], batch_id=manifest.get("batch_id"))
    return json.dumps(_persist_fraud_findings(findings), indent=2)


FRAUD_TOOLS = [
    score_batch_tool,
    score_local_batch_tool,
    detect_fraud_rings_tool,
    read_investigation_history_tool,
    query_fraud_db_tool,
    check_ofac_screen_tool,
    compute_fraud_score_tool,
] + FINDINGS_READ_TOOLS
