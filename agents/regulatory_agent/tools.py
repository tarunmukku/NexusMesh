"""Regulatory Browser Agent Tools (Agent 4)."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from storage.findings_store import write_findings

logger = logging.getLogger("nexusmesh.regulatory")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_PATH = REPO_ROOT / "data" / "naic_cache.json"
SCREENSHOTS_DIR = REPO_ROOT / "outputs" / "evidence_screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to load naic_cache.json: %s", exc)
    return {"bulletins": [], "state_reporting_requirements": []}


_NAIC_CACHE: dict = _load_cache()


def _slug(text: str) -> str:
    """Convert text to a filename-safe slug."""
    return re.sub(r"[^a-z0-9_-]", "_", text.lower())[:60]


@tool
def search_regulations_tool(query: str, states: str = "") -> str:
    """Search NAIC regulatory cache for bulletins and state reporting rules."""
    query_lower = query.lower()
    state_list = [s.strip().upper() for s in states.split(",") if s.strip()]

    keywords = set(query_lower.split())
    matched_bulletins = []
    for b in _NAIC_CACHE.get("bulletins", []):
        text = " ".join(
            [
                b.get("title", ""),
                b.get("summary", ""),
                b.get("key_requirement", ""),
                b.get("facts_relevance", ""),
            ]
        ).lower()
        if not keywords or len(keywords) <= 2 or any(kw in text for kw in keywords):
            matched_bulletins.append(b)

    ai_bulletin_ids = {b["id"] for b in matched_bulletins}
    for b in _NAIC_CACHE.get("bulletins", []):
        if (
            b.get("id") == "NAIC-Model-Bulletin-2023-AI"
            and b["id"] not in ai_bulletin_ids
        ):
            matched_bulletins.insert(0, b)

    reporting = _NAIC_CACHE.get("state_reporting_requirements", [])
    if state_list:
        reporting = [r for r in reporting if r.get("state", "").upper() in state_list]

    result = {
        "bulletins": matched_bulletins,
        "state_reporting_requirements": reporting,
        "source": "cache",
        "cached_on": _NAIC_CACHE.get("_meta", {}).get("cached_on", "2026-06-14"),
        "note": (
            "Results from local NAIC cache. In a live Band session Grok 4.3 will "
            "supplement this with real-time web browsing."
        ),
    }
    logger.info(
        "search_regulations_tool: query=%r states=%r → %d bulletins, %d state rules",
        query,
        states,
        len(matched_bulletins),
        len(reporting),
    )
    return json.dumps(result, indent=2)


@tool
def capture_evidence_screenshot_tool(url: str, claim_id: str) -> str:
    """Capture a headless browser screenshot of a regulatory URL as audit evidence."""
    slug = _slug(f"{claim_id}_{url}")
    png_path = SCREENSHOTS_DIR / f"{slug}.png"
    html_path = SCREENSHOTS_DIR / f"{slug}.html"

    try:
        import asyncio
        from playwright.async_api import async_playwright

        async def _screenshot() -> None:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(url, timeout=15_000, wait_until="domcontentloaded")
                await page.screenshot(path=str(png_path), full_page=True)
                await browser.close()

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    ex.submit(asyncio.run, _screenshot()).result(timeout=20)
            else:
                loop.run_until_complete(_screenshot())
        except RuntimeError:
            asyncio.run(_screenshot())

        logger.info("Playwright screenshot saved: %s", png_path)
        return json.dumps(
            {
                "screenshot_path": str(png_path),
                "url": url,
                "method": "playwright",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    except Exception as exc:
        logger.warning("Playwright screenshot failed (%s); writing HTML stub.", exc)

    stub = (
        f"<!DOCTYPE html><html><head><title>Evidence Stub — {claim_id}</title></head>"
        f"<body><h1>Regulatory Evidence</h1>"
        f"<p><strong>URL:</strong> <a href='{url}'>{url}</a></p>"
        f"<p><strong>Claim/Batch:</strong> {claim_id}</p>"
        f"<p><strong>Captured:</strong> {datetime.now(timezone.utc).isoformat()}</p>"
        f"<p><em>Playwright headless screenshot unavailable on this host. "
        f"View the source URL directly for audit evidence.</em></p>"
        f"</body></html>"
    )
    html_path.write_text(stub, encoding="utf-8")
    logger.info("HTML stub fallback saved: %s", html_path)
    return json.dumps(
        {
            "screenshot_path": str(html_path),
            "url": url,
            "method": "html_stub",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "note": "Playwright unavailable — HTML reference stub saved instead.",
        }
    )


@tool
def post_regulatory_findings_tool(
    batch_id: str,
    citations_json: str,
    state_requirements_json: str = "[]",
    compliance_alerts_json: str = "[]",
) -> str:
    """Persist regulatory findings to MongoDB so the Decision Agent can retrieve them."""
    batch_id = batch_id.strip()

    try:
        citations = (
            json.loads(citations_json)
            if isinstance(citations_json, str)
            else citations_json
        )
    except Exception:
        citations = []

    try:
        state_reqs = (
            json.loads(state_requirements_json)
            if isinstance(state_requirements_json, str)
            else state_requirements_json
        )
    except Exception:
        state_reqs = []

    try:
        alerts = (
            json.loads(compliance_alerts_json)
            if isinstance(compliance_alerts_json, str)
            else compliance_alerts_json
        )
    except Exception:
        alerts = []

    findings = {
        "message_type": "reg_citations",
        "batch_id": batch_id,
        "citations": citations,
        "state_reporting_requirements": state_reqs,
        "compliance_alerts": alerts,
        "summary": (
            f"{len(citations)} regulatory citation(s) found. "
            f"{len(state_reqs)} state reporting rule(s) applicable. "
            f"{len(alerts)} compliance alert(s)."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    persisted = False
    try:
        write_findings(
            batch_id,
            "reg_citations",
            findings,
            summary=findings["summary"],
            agent="regulatory_agent",
        )
        persisted = True
        logger.info(
            "Regulatory findings persisted for batch %s (%d citations)",
            batch_id,
            len(citations),
        )
    except Exception as exc:
        logger.warning("Failed to persist regulatory findings: %s", exc)

    return json.dumps(
        {
            "persisted": persisted,
            "batch_id": batch_id,
            "citation_count": len(citations),
            "state_rule_count": len(state_reqs),
            "alert_count": len(alerts),
            "summary": findings["summary"],
        },
        indent=2,
    )


REGULATORY_TOOLS = [
    search_regulations_tool,
    capture_evidence_screenshot_tool,
    post_regulatory_findings_tool,
]
