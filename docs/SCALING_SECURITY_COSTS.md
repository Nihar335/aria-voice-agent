# Scaling, Security, Infrastructure, Deployment & Costs

> All costs are **estimates** from public list prices (late 2025, USD → INR at ₹88) and must be re-checked before quoting to a client. Sample product prices are not official Fortivex AI prices.

## 1. What "1,00,000 users" means in load

Assumption: a voice call lasts **4 minutes**, with **8 customer turns**. Calls cluster in business hours, and the **peak hour carries about 12-15%** of a day's calls.

| Scenario | Calls / month | Peak calls per hour | **Concurrent calls at peak** | LLM requests / second at peak |
|---|---|---|---|---|
| A. 1 lakh users, ~1 call each per month | 1,00,000 | ~580 | **~40** | ~4 |
| B. 1 lakh calls **per day** (large rollout) | 30,00,000 | ~12,000 | **~800** | ~130 |

Formula: `concurrent = calls_per_hour × call_minutes / 60`. **Scenario A runs on a small cluster. Scenario B is what the architecture is designed for.**

## 2. How the system scales (MVP → production)

| Bottleneck in the MVP | Production fix |
|---|---|
| Sessions in a Python dict (one server only) | **Redis** with a time limit → servers become **stateless**, any pod can serve any turn, horizontal autoscaling |
| SQLite (one file, one writer) | **PostgreSQL** (managed, e.g. AWS RDS / Cloud SQL) with read replicas; `tenant_id` on every row for multi-client SaaS |
| NumPy vector search in memory | **pgvector** (or Qdrant) with one namespace per client; HNSW index keeps search ~10 ms at millions of chunks |
| Browser STT + HTTP per turn | **Telephony media streams** (Exotel / Twilio) over WebSocket → **streaming STT** with voice-activity detection → **streaming TTS** |
| Analysis as a FastAPI background task | **Queue** (Redis Streams / SQS) + **worker pool** that scales separately from live calls |
| Free-tier LLM keys | **Vertex AI / paid tiers with provisioned throughput**, several projects/keys in a pool, automatic failover to a second provider (already built in `llm.py`) |
| Polling for the cockpit | **WebSockets / Server-Sent Events** |

**Compute sizing for scenario B:** live calls are I/O-bound (waiting on STT/LLM/TTS), so one 2-vCPU pod handles about 80-100 concurrent streams. 800 concurrent ÷ 100 = 8 pods, plus 50% headroom = **~12 voice-gateway pods** and a similar number of agent pods, autoscaled on Kubernetes (GKE/EKS) or Cloud Run by concurrent connections.

**Latency targets (SLOs):** p95 time from customer stops speaking → agent starts speaking **< 1.2 s**; availability **99.9%**. How: stream LLM tokens straight into TTS sentence by sentence; Groq/Gemini Flash for the conversation; heavy models only in background workers; prompt caching for the fixed system prompt; India region (Mumbai) for low network delay.

**Reliability, already in the MVP and kept in production:**
| Failure | What happens |
|---|---|
| Main LLM down / rate-limited | `llm.py` retries on the backup provider in the same request |
| Embeddings API down | `rag.py` switches to keyword search |
| Neural TTS down | Browser / secondary TTS voice |
| All LLMs down | Polite apology + lead flagged `needs_human` in CRM |
| Server restart | (Production) sessions in Redis, so calls continue on another pod |

## 3. Security

| Area | MVP today | Production |
|---|---|---|
| Secrets | `.env`, git-ignored | Cloud secrets manager, key rotation, separate keys per environment |
| Transport | localhost | HTTPS/TLS everywhere, WSS for audio |
| Input validation | Pydantic: types, 600-char limit; phone normalised | + rate limiting per caller / IP, WAF |
| Database | Parameterised SQL (`?`), allow-listed columns | + encryption at rest, least-privilege DB users, backups |
| Web page | All text HTML-escaped (`esc()`) → no XSS | + CSP headers |
| Dashboard access | **None (demo only)** | SSO / OAuth login, role-based access (agent, supervisor, admin), audit log |
| Prompt injection | Agent can only use 5 tools; every tool validates arguments (e.g. slot must be truly free); LLM never touches the DB; RAG reads only approved company docs | + output filters, red-team test set, per-tenant tool permissions |
| Hallucination | RAG with similarity threshold, sources shown, "never invent prices" rule, handoff when unsure | + automatic evaluation on a test set of real questions before every release |
| PII | Phone/email stored locally | PII masking in logs, retention policy (e.g. delete recordings after 90 days), data residency in India |
| Compliance (India) | — | **DPDP Act 2023** (consent, purpose limitation, right to erasure); AI + recording disclosure at call start; **TRAI TCCCPR** DND rules and registered headers for outbound calls; NDA / DPA with clients |

## 4. Infrastructure & deployment

**MVP (today, ~₹0/month):** single container → Render / Railway / Google Cloud Run free tier. `Dockerfile` included:
```
docker build -t aria .
docker run -p 8000:8000 --env-file .env aria
```

**Production:**
- Cloud: GCP (Vertex AI for Gemini, data in Mumbai `asia-south1`) or AWS (`ap-south-1`).
- Containers on Kubernetes / Cloud Run, autoscaling by concurrent calls.
- CI/CD: GitHub Actions → tests + prompt evaluation set → build image → deploy to staging → canary to production.
- Infrastructure as code: Terraform.
- Observability: OpenTelemetry traces per call (STT → RAG → LLM → tools → TTS), Grafana dashboards for p95 latency and error rate, LLM tracing (e.g. Langfuse) for prompt debugging, alerts on latency or handoff spikes.

## 5. Cost per call (unit economics)

Per 4-minute call, about 37k LLM input tokens and 3.4k output tokens (agent + per-turn analysis + summary):

| Component | Basis (list prices, approx.) | ₹ per call |
|---|---|---|
| LLM (Gemini 2.5 Flash class) | ~$0.30 / 1M input, ~$2.50 / 1M output tokens | ~1.7 |
| Streaming STT | ~$0.006-0.008 / minute | ~2.5 |
| Neural TTS | ~$16 / 1M characters, ~1,500 chars per call | ~2.1 |
| Telephony (India) | ~₹0.5-1.0 / minute | ~3.0 |
| Infra share (compute, DB, Redis, storage, monitoring) | | ~0.5 |
| **Total** | | **≈ ₹10 per call (₹2.5 / minute)** |

**Monthly cost:**
| | Scenario A (1 lakh calls/month) | Scenario B (30 lakh calls/month) |
|---|---|---|
| Variable (₹10 × calls) | ₹10 lakh | ₹3 crore |
| Fixed infra (cluster, managed DB, Redis, monitoring) | ~₹1.5 lakh | ~₹8-10 lakh |
| **Total** | **~₹11.5 lakh (₹11.5 / call)** | **~₹3.1 crore (₹10.3 / call)** |

**Cost levers (can bring it to ~₹5-6 per call):** cheaper model (Flash-Lite) for per-turn analysis or analyse every 2nd turn; prompt/context caching of the fixed system prompt; self-hosted Indic STT/TTS (e.g. AI4Bharat models) on GPUs at high volume; negotiated telephony rates; caching answers to the most common questions.

## 6. Commercial feasibility

| | Human call executive | Aria (sample Growth plan) |
|---|---|---|
| Monthly cost to client | ~₹35-40k (salary + seat + telecom + supervision) | ₹45,000 |
| Calls handled | ~1,200 (office hours) | 2,000 (24x7) |
| Cost per call | ~₹30 | **~₹22.5** |
| Languages | 1-2 | English, Hindi, Hinglish, Kannada, Telugu (+ more) |
| Lead scoring + CRM entry + summary | manual | automatic, every call |

- **Fortivex gross margin:** price ~₹22.5 vs cost ~₹10 → **~55%**, before cost optimisations; plus one-time setup fees (₹25-60k) cover onboarding.
- **Client ROI:** the main value is not only cheaper calls but **no missed leads** (after-hours calls, peak-time overflow) and faster follow-up. A clinic or real-estate firm that converts even 2-3 extra leads a month pays for the plan.
- **Fit with Fortivex's portfolio:** sold as an add-on to **Fortivex CRM** (adapter already designed), and as a vertical product for **Medivex AI** (clinic appointment booking) and **Empowers Academy** (course enquiries and admissions).

## 7. Honest MVP limitations
- No login on the dashboard; one tenant; sessions in memory; SQLite.
- Browser STT works in Chrome/Edge only; edge-tts is an unofficial free service (no SLA).
- Free-tier LLM rate limits → fine for a demo, not for production traffic.
- Turn latency ~2-4 s with the natural voice (no streaming yet).
All of these have a planned fix in sections 2-4.
