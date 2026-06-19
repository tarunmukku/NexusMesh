# NexusMesh Guard

**NexusMesh Guard** is an auditable, multi-agent AI claims fraud detection and compliance platform built for the US auto insurance sector. It uses a **6-agent hybrid architecture** coordinated via the **Band SDK** and powered exclusively by the **AI/ML API**.

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![Band SDK](https://img.shields.io/badge/Band-SDK-brightgreen.svg)](https://band.ai)
[![AI/ML API](https://img.shields.io/badge/AI%2FML-API-purple.svg)](https://aimlapi.com)

---

## The Problem: A $308 Billion Crisis

Insurance fraud costs US consumers and carriers over **$308 billion annually** (Coalition Against Insurance Fraud). In markets like Florida, staged "swoop-and-squat" accidents and Personal Injury Protection (PIP) mills represent massive leakages. 

Currently, carriers rely on legacy rules engines or black-box ML models (like Shift Technology) that generate risk scores without explainability. Furthermore, the NAIC has adopted the **Model Bulletin on the Use of AI Systems by Insurers**, requiring strict governance over AI decisions. 

**NexusMesh Guard solves this by deploying a fully auditable swarm of specialized AI agents that execute forensic investigations, cross-reference state compliance, detect fraud rings, and present actionable, explainable findings to a Human-in-the-Loop (HITL) compliance officer.**

---

## Multi-Agent Architecture

NexusMesh uses the **Band SDK** (via LangGraph adapters) to coordinate a stateful, parallel-to-serial flow. Six distinct Remote Agents operate inside a secure Band collaboration room:

1. **Intake Agent**: Parses CSVs/PDFs, performs OCR, and streams FNOL claim manifests to MongoDB.
2. **Document Authenticity Agent**: Executes a 4-layer forensic check (EXIF, C2PA bytes, JPEG Error-Level-Analysis, and Vision-LM inspection) to detect AI-generated deepfakes.
3. **Fraud Detection Agent**: Uses the Šubelj graph-clustering algorithm to detect multi-claim fraud rings (e.g., shared tow companies and providers). 
4. **Regulatory Browser Agent**: Uses live web search (Grok) and headless Playwright to fetch current NAIC/State DOI bulletins and take screenshots for evidence.
5. **Policy Risk Analyzer**: Extracts clauses from ISO policy PDFs to ensure the policy meets state statutory minimums (e.g., Florida PIP requirements).
6. **Decision & Governance Agent**: Waits for all analysis agents to report back (via a `MentionGate` barrier), aggregates the findings, enforces traffic-light routing (Green, Yellow, Red), and generates FACTS-compliant PDFs. It interrupts execution to demand **Human-in-the-Loop (HITL)** approval for RED claims.

### The Band Coordination Layer

Band is not just a logger; it is the **asynchronous state machine**.
The agents execute in parallel. To prevent infinite loops or premature processing, they adhere to an 8-message Domain Protocol. The analysis agents silently dump their rich JSON payloads to MongoDB, but use `thenvoi_send_message` to post short, @mention-routed markers in the Band room. The Decision Agent is gated behind a barrier until it receives exactly 4 specific mentions.

---

## Powered by AI/ML API

We used **AI/ML API** exclusively as the single routing endpoint for all 6 agents. To ensure maximal capability and avoid single-vendor hallucinations, we deployed **6 different models across 5 different vendors**:

- **Google (gemini-2.5-flash)**: Intake Agent (Fast, highly accurate JSON extraction + Multimodal vision OCR for scanned forms).
- **Alibaba (qwen3.5-omni-plus)**: Doc Authenticity Agent (State-of-the-art vision reasoning to spot deepfake anomalies in car damage photos).
- **MiniMax (minimax-m3)**: Fraud Agent (Massive context window allows few-shot reasoning over historic SIU investigation outcomes).
- **xAI (grok-4-3)**: Regulatory Browser (Native, un-censored live web access to pull real-time state compliance mandates).
- **OpenAI (gpt-5.1-2025-11-13)**: Policy Agent (Incredible precision in parsing and cross-referencing dense ISO policy PDF clauses).
- **OpenAI (gpt-5-2-chat-latest)**: Decision Agent (The highest-capability reasoning model handles the complex LangGraph HITL state interruptions and generates the final governance PDF).

---

## FACTS Compliance Layer (NAIC Model Bulletin)

Unlike competitors like FRISS, NexusMesh Guard implements a transparent **FACTS** governance layer to comply with the NAIC AI Model Bulletin:

- **Fairness**: Streamlit Dashboard displays a real-time disparate-impact snapshot (ensuring flag rates aren't biased by state or zip code).
- **Accountability**: Every decision is cryptographically logged with the specific AI/ML API model string and the designated human SIU officer who approved the escalation.
- **Compliance**: The Policy Agent checks all coverages against the Unfair Claims Settlement Practices Act (UCSPA).
- **Transparency**: Generates plain-English consumer notices for adverse actions.
- **Safety**: Automatically benchmarks its Document Authenticity model against a planted Kaggle deepfake dataset to report its active False Positive rate.

---

## Data Sourcing

For demonstration and testing purposes:
- **Real Data**: The OFAC SDN sanctions list is an authentic subset. The Kaggle Car Insurance Fraud dataset is used for deepfake benchmarking.
- **Mocked Data**: The core `sample_claims.csv` is handcrafted to explicitly test our systems (e.g., planting `RING-001` with shared tow companies, and `CLM-0019` mapping to a real OFAC hit).

---

## Production Path

NexusMesh Guard is designed as an overlay, not a rip-and-replace. In production, the system would plug directly into **Guidewire ClaimCenter** or **Duck Creek** via REST APIs, replacing the `parse_csv_tool` with webhooks. 

The `query_fraud_db` tool would be swapped with a live integration to **ISO ClaimSearch** to enable nationwide ring detection.

