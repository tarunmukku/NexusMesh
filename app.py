import streamlit as st
import pandas as pd
import numpy as np
import os, glob, math, json
from datetime import datetime
from dotenv import load_dotenv
from pymongo import MongoClient
import plotly.graph_objects as go

load_dotenv()
st.set_page_config(
    page_title="NexusMesh Guard",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="collapsed",
)
# ---------------------------------------------------------------- CSS
st.markdown(
    """
<style>
  .metric-card{background:#F8F9FA;padding:16px;border-radius:10px;text-align:center;
               box-shadow:0 2px 4px rgba(0,0,0,.1);height:100%;border:1px solid #E0E0E0;}
  .metric-value{font-size:2.0rem;font-weight:bold;color:#333;}
  .metric-label{font-size:.78rem;color:#666;text-transform:uppercase;letter-spacing:1px;}
  .agent-badge{display:inline-block;padding:4px 8px;border-radius:4px;background:#E9ECEF;
               font-size:.8rem;margin-right:5px;border:1px solid #CED4DA;color:#333;}
  .pill{display:inline-block;padding:2px 10px;border-radius:12px;font-weight:bold;font-size:.85rem;}
</style>
""",
    unsafe_allow_html=True,
)
RED, AMBER, GREEN = "#FF4B4B", "#FFA500", "#2ECC71"
TIER_COLOR = {"RED": RED, "AMBER": AMBER, "GREEN": GREEN}


# ---------------------------------------------------------------- DB
@st.cache_resource
def get_db():
    from agents.intake_agent.data_store import build_mongo_uri, DEFAULT_DB_NAME

    uri = build_mongo_uri()
    if not uri:
        st.error("No MongoDB URI found. Check your .env file.")
        st.stop()
    return MongoClient(uri)[DEFAULT_DB_NAME]


db = get_db()
# ---------------------------------------------------------------- HEADER
c_title, c_btn = st.columns([4, 1])
with c_title:
    st.title("🛡️ NexusMesh Guard")
    st.markdown("##### Multi-Agent Fraud Detection & Compliance Platform")
with c_btn:
    st.write("")
    st.write("")
    if st.button("🔄 Refresh Live Data", use_container_width=True, type="primary"):
        st.rerun()
_AGENTS_UI = [
    (
        "📥",
        "Intake",
        "Ingests & streams the batch",
        "gemini-2.5-flash",
        "Google",
        "#0070C0",
    ),
    (
        "📸",
        "Doc Auth",
        "Multimodal image forensics",
        "qwen3.5-omni-plus",
        "Alibaba",
        "#FF9900",
    ),
    ("🕸️", "Fraud", "Šubelj ring & pattern match", "minimax-m3", "MiniMax", "#C00000"),
    ("📜", "Regulatory", "Live DOI statute search", "grok-4-3", "xAI", "#00994C"),
    ("⚖️", "Policy", "ISO coverage-limit checks", "gpt-5.1", "OpenAI", "#7030A0"),
    ("🧠", "Decision", "Aggregates + audit report", "gpt-5.2", "OpenAI", "#5A5A6E"),
]
with st.expander("Agent Architecture & Model Routing — Powered by AI/ML API", expanded=True):
    tiles = "".join(
        f"""<div style="flex:1 1 30%;min-width:175px;background:linear-gradient(135deg,#FFFFFF,#F8F9FA);
                 border-left:4px solid {col};border-radius:10px;padding:12px 14px;box-shadow:0 2px 4px rgba(0,0,0,.05);">
              <div style="font-size:1.3rem;line-height:1">{icon}</div>
              <div style="font-weight:700;color:#333;margin-top:4px">{name}</div>
              <div style="font-size:.72rem;color:#666">{role}</div>
              <div style="margin-top:7px">
                <code style="background:#F1F3F5;color:{col};padding:2px 7px;border-radius:5px;font-size:.72rem">{model}</code>
              </div>
              <div style="font-size:.66rem;color:#888;margin-top:3px">via AI/ML API · {prov}</div>
            </div>"""
        for icon, name, role, model, prov, col in _AGENTS_UI
    )
    st.markdown(
        f"""
        <div style="display:flex;flex-wrap:wrap;gap:10px">{tiles}</div>
        """,
        unsafe_allow_html=True,
    )
st.divider()


# ---------------------------------------------------------------- LOAD
def load_data():
    latest = db["claim_manifests"].find_one(sort=[("_id", -1)])
    if not latest:
        return None, [], {}, {}, {}, {}, []
    batch_id = latest.get("batch_id")
    manifests = list(db["claim_manifests"].find({"batch_id": batch_id}))

    def finding(ftype):
        doc = db["agent_findings"].find_one(
            {"batch_id": batch_id, "message_type": ftype}
        )
        return doc.get("payload", {}) if doc else {}

    fraud = finding("fraud_findings")
    doc_auth = finding("doc_authenticity")
    policy = finding("policy_risks")
    reg = finding("reg_citations")
    history = list(db["investigation_history"].find(sort=[("timestamp", -1)]).limit(25))
    return batch_id, manifests, fraud, doc_auth, policy, reg, history


batch_id, manifests, fraud, doc_auth, policy, reg, history = load_data()
if not batch_id:
    st.warning("⚠️ No claims processed yet. Run the workflow in the Band Chat UI.")
    st.stop()


# ---------------------------------------------------------------- DECISION LOGIC (mirrors decision_agent.aggregate_claim_risk)
def combined_for(fraud_score, verdict, auth_score):
    """Replicate the Decision Agent: combined_risk = max(fraud, deepfake penalty)."""
    is_deepfake = verdict == "DEEPFAKE_DETECTED"
    is_susp = verdict == "SUSPICIOUS"
    doc_penalty = (
        100.0 * (1.0 - auth_score) if (is_deepfake and auth_score is not None) else 0.0
    )
    score = max(float(fraud_score or 0), doc_penalty)
    score = min(100.0, max(0.0, score))
    if score >= 75 or is_deepfake:
        tier = "RED"
    elif score >= 30 or is_susp:
        tier = "AMBER"
    else:
        tier = "GREEN"
    return round(score, 1), tier, is_deepfake, is_susp


# index agent findings by claim
flagged = {f["claim_id"]: f for f in fraud.get("flagged_claims", [])}
docs = {
    d["claim_id"]: d
    for d in doc_auth.get("documents_analyzed", [])
    if d.get("claim_id")
}
hitl = {}
for h in history:
    cid = h.get("claim_id")
    if cid and cid not in hitl:
        hitl[cid] = h.get("officer_decision")
rows, total_exposure, exposure_red, exposure_flagged = [], 0.0, 0.0, 0.0
red = amber = green = deepfakes = suspicious = 0
for m in manifests:
    c = (m.get("claims") or [{}])[0]
    cid = c.get("claim_id")
    amount = c.get("claimed_amount") or 0
    total_exposure += amount
    fi = flagged.get(cid, {})
    fscore = fi.get("fraud_score", 0)
    reasons = fi.get("reason_codes", [])  # FIX: was "reasons"
    di = docs.get(cid, {})
    verdict = di.get("verdict", "AUTHENTIC")
    auth = di.get("authenticity_score")
    score, tier, is_df, is_sp = combined_for(fscore, verdict, auth)
    if tier == "RED":
        red += 1
        exposure_red += amount
        exposure_flagged += amount
    elif tier == "AMBER":
        amber += 1
        exposure_flagged += amount
    else:
        green += 1
    if is_df:
        deepfakes += 1
    if is_sp:
        suspicious += 1
    rationale = []
    if fscore:
        rationale.append(
            f"Fraud {fscore}" + (f" ({', '.join(reasons[:2])})" if reasons else "")
        )
    if is_df:
        rationale.append(
            f"Deepfake (auth {auth:.2f})" if auth is not None else "Deepfake"
        )
    elif is_sp:
        rationale.append(
            f"Image suspicious (auth {auth:.2f})"
            if auth is not None
            else "Image suspicious"
        )
    rationale = "; ".join(rationale) or "All checks nominal"
    hstatus = "—"
    if tier == "RED":
        hstatus = hitl.get(cid, "⏳ Pending review")
    rows.append(
        {
            "Tier": tier,
            "Claim ID": cid,
            "Risk": score,
            "Amount": amount,
            "State": c.get("state"),
            "Loss Type": c.get("loss_type"),
            "Image": {
                "DEEPFAKE_DETECTED": "🔴 Deepfake",
                "SUSPICIOUS": "🟠 Suspicious",
            }.get(verdict, "✅ Verified"),
            "HITL": hstatus,
            "Why (Decision Agent)": rationale,
            "_reasons": reasons,
            "_doc": di,
            "_fraud": fi,
            "_auth": auth,
            "_verdict": verdict,
        }
    )
df = pd.DataFrame(rows)
df["_sort"] = df["Tier"].map({"RED": 0, "AMBER": 1, "GREEN": 2})
df = (
    df.sort_values(["_sort", "Risk"], ascending=[True, False])
    .drop(columns=["_sort"])
    .reset_index(drop=True)
)
policy_overall = policy.get("overall_risk", "—")
rings = fraud.get("rings_detected", [])
stp_rate = (green / len(manifests) * 100) if manifests else 0
# ---------------------------------------------------------------- KPI ROW
st.markdown("### Batch Risk Overview")
st.markdown(f"**Batch ID:** `{batch_id}`")


def card(col, value, label, color="#333"):
    col.markdown(
        f"<div class='metric-card'><div class='metric-value' style='color:{color};'>"
        f"{value}</div><div class='metric-label'>{label}</div></div>",
        unsafe_allow_html=True,
    )


k = st.columns(6)
card(k[0], len(manifests), "Total Claims")
card(k[1], f"${total_exposure:,.0f}", "Total Exposure")
card(k[2], f"${exposure_red:,.0f}", "Exposure in RED", RED)
card(k[3], red, "RED (HITL)", RED)
card(k[4], deepfakes, "Deepfakes", RED if deepfakes else "#333")
card(k[5], f"{stp_rate:.0f}%", "Straight-Through", GREEN)
k2 = st.columns(6)
card(k2[0], amber, "AMBER", AMBER)
card(k2[1], green, "GREEN (auto)", GREEN)
card(k2[2], suspicious, "Suspect Images", AMBER if suspicious else "#333")
card(k2[3], len(rings), "Fraud Rings", RED if rings else "#333")
card(
    k2[4],
    policy_overall,
    "Policy Risk",
    RED if policy_overall in ("Critical", "High") else "#333",
)
card(k2[5], f"${exposure_flagged:,.0f}", "Flagged Exposure", AMBER)
st.write("")
st.write("")
# ---------------------------------------------------------------- GRID
st.markdown(
    "### Claim Risk Register  ·  *risk tier assigned by the Decision Agent (fraud + image forensics)*"
)
grid = df.drop(columns=["_reasons", "_doc", "_fraud", "_auth", "_verdict"])


def color_tier(v):
    if v == "RED":
        return "background:rgba(255,75,75,.20);color:#FF4B4B;font-weight:bold;"
    if v == "AMBER":
        return "background:rgba(255,165,0,.20);color:#FFA500;font-weight:bold;"
    return "background:rgba(46,204,113,.12);color:#2ECC71;"


st.dataframe(
    grid.style.map(color_tier, subset=["Tier"]).format(
        {"Amount": "${:,.2f}", "Risk": "{:.0f}"}
    ),
    use_container_width=True,
    hide_index=True,
    height=380,
)
# ---------------------------------------------------------------- PER-CLAIM DRILL-DOWN
st.markdown("### Claim Investigation Detail  ·  *consolidated view across all agents*")
sel = st.selectbox("Select a claim", df["Claim ID"].tolist(), index=0)
r = df[df["Claim ID"] == sel].iloc[0]
d1, d2, d3 = st.columns(3)
with d1:
    st.markdown(
        f"<span class='pill' style='background:{TIER_COLOR[r['Tier']]};color:#111;'>"
        f"{r['Tier']} · risk {r['Risk']:.0f}</span>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"**Amount:** ${r['Amount']:,.0f}  \n**State:** {r['State']}  \n**Loss:** {r['Loss Type']}"
    )
    st.markdown(f"**HITL:** {r['HITL']}")
with d2:
    st.markdown("**Fraud Agent**")
    if r["_reasons"]:
        for rc in r["_reasons"]:
            st.markdown(f"- {rc}")
    else:
        st.caption("No fraud patterns matched.")
with d3:
    st.markdown("**Document Authenticity**")
    st.markdown(
        f"Verdict: **{r['_verdict']}**"
        + (f"  ·  score {r['_auth']:.2f}" if r["_auth"] is not None else "")
    )
    ev = (r["_doc"] or {}).get("evidence")
    if ev:
        st.caption(ev)
st.info(f"**Decision Agent rationale:** {r['Why (Decision Agent)']}")
st.divider()
# ---------------------------------------------------------------- TABS
tab1, tab2, tab3, tab4 = st.tabs(
    [
        "Network & Ring Analysis",
        "Document Forensics",
        "Policy & Regulatory",
        "Governance & Fairness",
    ]
)
# ===== TAB 1: RING NETWORK GRAPH =====
with tab1:
    st.markdown("#### Shared-Entity Network Analysis (Šubelj clustering)")
    if rings:
        st.success(f"🚨 {len(rings)} network cluster(s) detected")
        tier_of = dict(zip(df["Claim ID"], df["Tier"]))
        entities = sorted({e for r in rings for e in r.get("shared_entities", [])})
        members = sorted({m for r in rings for m in r.get("members", [])})
        pos = {}
        for i, e in enumerate(entities):
            a = 2 * math.pi * i / max(len(entities), 1)
            pos[("E", e)] = (math.cos(a) * 1.0, math.sin(a) * 1.0)
        for i, c in enumerate(members):
            a = 2 * math.pi * i / max(len(members), 1)
            pos[("C", c)] = (math.cos(a) * 2.8, math.sin(a) * 2.8)
        ex, ey = [], []
        for ring in rings:
            for m in ring.get("members", []):
                for e in ring.get("shared_entities", []):
                    if ("C", m) in pos and ("E", e) in pos:
                        ex += [pos[("C", m)][0], pos[("E", e)][0], None]
                        ey += [pos[("C", m)][1], pos[("E", e)][1], None]
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=ex,
                y=ey,
                mode="lines",
                line=dict(color="rgba(150,150,170,.4)", width=1),
                hoverinfo="none",
                showlegend=False,
            )
        )
        # entity nodes
        fig.add_trace(
            go.Scatter(
                x=[pos[("E", e)][0] for e in entities],
                y=[pos[("E", e)][1] for e in entities],
                mode="markers+text",
                text=entities,
                textposition="top center",
                marker=dict(
                    symbol="diamond",
                    size=18,
                    color="#6C5CE7",
                    line=dict(width=1, color="#FFF"),
                ),
                hovertext=[f"Shared entity: {e}" for e in entities],
                hoverinfo="text",
                name="Shared entity",
            )
        )
        # claim nodes
        fig.add_trace(
            go.Scatter(
                x=[pos[("C", c)][0] for c in members],
                y=[pos[("C", c)][1] for c in members],
                mode="markers+text",
                text=members,
                textposition="bottom center",
                marker=dict(
                    size=22,
                    color=[
                        TIER_COLOR.get(tier_of.get(c, "AMBER"), AMBER) for c in members
                    ],
                    line=dict(width=1.5, color="#FFF"),
                ),
                hovertext=[f"{c} · {tier_of.get(c,'?')}" for c in members],
                hoverinfo="text",
                name="Claim",
            )
        )
        fig.update_layout(
            showlegend=True,
            height=460,
            margin=dict(l=10, r=10, t=10, b=10),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(visible=False),
            yaxis=dict(visible=False),
            legend=dict(bgcolor="rgba(30,30,46,.6)"),
        )
        st.plotly_chart(fig, use_container_width=True)
        for ring in rings:
            with st.expander(
                f"🕸️ {ring.get('ring_id','Ring')} — {len(ring.get('members',[]))} claims, "
                f"{len(ring.get('shared_entities',[]))} shared entities"
            ):
                st.write(f"**Claims:** {', '.join(ring.get('members', []))}")
                st.markdown("**Shared entities:**")
                for e in ring.get("shared_entities", []):
                    st.markdown(f"- `{e}`")
    else:
        st.info("No active fraud rings detected.")
# ===== TAB 2: DOC AUTHENTICITY =====
with tab2:
    st.markdown("#### Image Authenticity Forensics  ·  EXIF · C2PA · ELA · Vision")
    flagged_docs = [
        d
        for d in doc_auth.get("documents_analyzed", [])
        if d.get("verdict") in ("DEEPFAKE_DETECTED", "SUSPICIOUS")
    ]
    if not flagged_docs:
        st.success(
            "All attached claim images passed metadata, C2PA, ELA and vision checks."
        )
    for d in flagged_docs:
        cid = d.get("claim_id", "?")
        verdict = d.get("verdict")
        score = d.get("authenticity_score", 0) or 0
        icon = "🔴" if verdict == "DEEPFAKE_DETECTED" else "🟠"
        with st.expander(
            f"{icon} {cid} — {verdict}  ·  authenticity {score:.2f}",
            expanded=(verdict == "DEEPFAKE_DETECTED"),
        ):
            g, info = st.columns([1, 2])
            with g:
                gauge = go.Figure(
                    go.Indicator(
                        mode="gauge+number",
                        value=score * 100,
                        domain={"x": [0, 1], "y": [0, 1]},
                        number={"suffix": "%"},
                        gauge={
                            "axis": {"range": [0, 100]},
                            "bar": {
                                "color": (
                                    RED
                                    if score < 0.5
                                    else (AMBER if score < 0.85 else GREEN)
                                )
                            },
                            "steps": [
                                {"range": [0, 50], "color": "rgba(255,75,75,.25)"},
                                {"range": [50, 85], "color": "rgba(255,165,0,.25)"},
                                {"range": [85, 100], "color": "rgba(46,204,113,.25)"},
                            ],
                        },
                    )
                )
                gauge.update_layout(
                    height=200,
                    margin=dict(l=10, r=10, t=10, b=10),
                    paper_bgcolor="rgba(0,0,0,0)",
                )
                st.plotly_chart(gauge, use_container_width=True)
            with info:
                if d.get("evidence"):
                    st.markdown(f"**Evidence:** {d['evidence']}")
                layers = d.get("layers", {})
                exf = layers.get("exif", {})
                c2 = layers.get("c2pa", {})
                comp = layers.get("compression", {})
                vis = layers.get("vision", {})
                lc1, lc2 = st.columns(2)
                with lc1:
                    st.markdown(
                        f"**EXIF** — {', '.join(exf.get('flags', [])) or 'nominal'}"
                    )
                    st.markdown(
                        f"**C2PA** — {'present' if c2.get('has_c2pa') else 'absent'}; "
                        f"{', '.join(c2.get('flags', [])) or '—'}"
                    )
                with lc2:
                    ela = []
                    if comp.get("ela_max") is not None:
                        ela.append(f"peak {comp['ela_max']}")
                    if comp.get("ela_mean") is not None:
                        ela.append(f"mean {comp['ela_mean']}")
                    st.markdown(
                        f"**ELA** — {', '.join(ela) or '—'}; {', '.join(comp.get('flags', [])) or 'nominal'}"
                    )
                    vtxt = (
                        "unavailable"
                        if not vis.get("available")
                        else (", ".join(vis.get("tells", [])) or "no tells")
                    )
                    st.markdown(f"**Vision** — {vtxt}")
# ===== TAB 3: POLICY & REGULATORY =====
with tab3:
    pcol, rcol = st.columns(2)
    with pcol:
        st.markdown(
            f"#### Policy Compliance Assessment  ·  overall risk: "
            f"<span class='pill' style='background:{RED if policy_overall in ('Critical','High') else AMBER};"
            f"color:#111;'>{policy_overall}</span>",
            unsafe_allow_html=True,
        )
        gaps = policy.get("coverage_gaps", [])  # FIX: real key
        if gaps:
            gdf = pd.DataFrame(
                [
                    {
                        "Clause": g.get("clause_type"),
                        "Severity": g.get("severity"),
                        "Finding": g.get("finding"),
                        "NAIC Ref": g.get("naic_reference"),
                    }
                    for g in gaps
                ]
            )
            st.dataframe(gdf, use_container_width=True, hide_index=True)
        else:
            st.success("No coverage gaps — policy meets statutory minimums.")
        rbc = policy.get("rbc_check", {})
        if rbc:
            st.caption(
                f"RBC ratio: {rbc.get('rbc_ratio','?')}%  ·  status {rbc.get('status','?')}"
            )
        for lf in policy.get("language_flags", []):
            st.caption(f"• {lf}")
    with rcol:
        st.markdown("#### State Regulatory Requirements")
        for req in reg.get("state_reporting_requirements", []):
            with st.container(border=True):
                st.markdown(
                    f"**{req.get('state','?')}** — {req.get('reporting_body','')}"
                )
                if req.get("trigger"):
                    st.caption(f"Trigger: {req['trigger']}")
                if req.get("timeframe"):
                    st.caption(f"Timeframe: {req['timeframe']}")
        for alert in reg.get("compliance_alerts", []):
            st.warning(alert)
        cites = reg.get("citations", [])
        if cites:
            st.markdown("**Sources (audit trail):**")
            for c in cites:
                title = c.get("title") or c.get("name") or c.get("source") or "citation"
                url = c.get("url") or c.get("source_url")
                st.markdown(f"- [{title}]({url})" if url else f"- {title}")
        shots = glob.glob("outputs/evidence_screenshots/*")
        if shots:
            with st.expander(f"📎 Captured regulatory evidence ({len(shots)} file(s))"):
                for s in shots:
                    st.markdown(f"- `{os.path.basename(s)}`")
        try:
            nb = json.load(open("data/naic_cache.json")).get("bulletins", [])
            if nb:
                st.markdown("**Governing bulletin:**")
                for b in nb[:2]:
                    st.markdown(
                        f"- [{b.get('title','NAIC bulletin')}]({b.get('url','')}) "
                        f"({b.get('effective_date','')})"
                    )
        except Exception:
            pass
# ===== TAB 4: FACTS GOVERNANCE =====
with tab4:
    st.markdown("#### Fairness & Disparate-Impact Analysis")
    st.caption(
        "Flag rates by state are tracked for the NAIC AI Model Bulletin. "
        "Combined tier (Decision Agent) is used, not fraud score alone."
    )
    by_state = (
        df.groupby("State")
        .agg(
            total=("Claim ID", "count"),
            flagged=("Tier", lambda s: (s != "GREEN").sum()),
        )
        .reset_index()
    )
    by_state["rate"] = (by_state["flagged"] / by_state["total"] * 100).round(1)
    by_state["low_n"] = by_state["total"] < 5
    fig = go.Figure(
        go.Bar(
            x=by_state["State"],
            y=by_state["rate"],
            marker_color=[
                "rgba(255,75,75,.5)" if ln else RED for ln in by_state["low_n"]
            ],
            text=[
                f"{rt}%<br>n={n}" for rt, n in zip(by_state["rate"], by_state["total"])
            ],
            textposition="outside",
        )
    )
    fig.update_layout(
        height=320,
        margin=dict(l=10, r=10, t=10, b=10),
        yaxis_title="Flag rate (%)",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(fig, use_container_width=True)
    if bool(by_state["low_n"].any()):
        st.caption(
            "⚠️ Faded bars are states with < 5 claims — shown for transparency, "
            "not treated as a statistically reliable bias signal."
        )
    hm = [
        p for p in glob.glob("outputs/heatmaps/*.png") if batch_id.lower() in p.lower()
    ]
    if hm:
        st.image(
            hm[0], caption="Decision Agent fairness heatmap", use_container_width=True
        )
    pdfs = [
        p for p in glob.glob("outputs/reports/*.pdf") if batch_id.lower() in p.lower()
    ]
    if pdfs:
        with open(pdfs[0], "rb") as f:
            st.download_button(
                "📄 Download Official FACTS Audit Report (PDF)",
                data=f,
                file_name=os.path.basename(pdfs[0]),
                mime="application/pdf",
                use_container_width=True,
            )
st.divider()
# ---------------------------------------------------------------- SIU FEEDBACK
st.markdown("### Investigator Feedback Loop")
st.caption(
    f"{len(history)} past outcomes logged by officers — this data retrains the Fraud Agent."
)
if history:
    hdf = pd.DataFrame(history)
    keep = [
        c
        for c in ["claim_id", "officer_decision", "officer_reason", "timestamp"]
        if c in hdf.columns
    ]
    hdf = hdf[keep]
    if "timestamp" in hdf:
        hdf["timestamp"] = pd.to_datetime(
            hdf["timestamp"], errors="coerce"
        ).dt.strftime("%Y-%m-%d %H:%M")
    st.dataframe(hdf, use_container_width=True, hide_index=True)
else:
    st.info("No historical decisions recorded yet.")
