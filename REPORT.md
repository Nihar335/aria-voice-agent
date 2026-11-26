# Aria: Autonomous AI Sales Voice Agent (1-page report)

**Author:** Nihar · **Built for:** Fortivex AI Pvt Ltd assignment · **Stack:** Python, FastAPI, Groq (Qwen, GPT-OSS) + Gemini, RAG, edge-tts, SQLite, vanilla JS

**Business problem.** Indian SMBs (clinics, real estate, education, services) miss a large share of inbound enquiries: calls after hours, at peak times, or in a language the executive does not speak. Leads that do get answered are qualified inconsistently and rarely logged in a CRM with notes. Every missed or poorly handled call is lost revenue.

**Solution.** Aria is a voice agent that answers like a trained sales executive, 24x7, in **English, Hindi, Hinglish, Kannada and Telugu**. It answers only from the company's approved documents (with sources), qualifies the lead (BANT), handles objections, **books a demo** into the calendar, writes everything into a **CRM**, hands over to a human when needed, and remembers returning callers. The demo business is Fortivex AI itself (sample prices).

**The 6 required capabilities, all working end to end:**
| Capability | Implementation |
|---|---|
| Real-time voice (STT/TTS) | Browser records the mic and detects speech → server speech-to-text (Groq Whisper for English/Hindi, Gemini for Telugu/Kannada/Hinglish, each backing up the other) → agent → Gemini TTS for Telugu, edge-tts neural Indian voices for the rest; barge-in; browser-voice fallback |
| Memory + multilingual | Conversation history per call; returning caller recognised by phone number → last call summary loaded; script-based language detection, auto-switching voice and mic |
| Lead qualification + sentiment | LLM extracts BANT facts + sentiment each turn (background); **score = transparent formula** (Need 35, Budget 25, Authority 20, Timeline 20); upset-customer alert |
| RAG knowledge | Section chunks → Gemini embeddings → cosine search with a **minimum-relevance threshold** (no guessing); TF-IDF offline fallback; sources shown |
| Appointment booking + CRM | Agentic tool loop: `check_availability` → `book_appointment` (slot validated by code) → Google Calendar link; CRM behind an **adapter** (SQLite today, Fortivex CRM = one class) |
| Call summary + insights | End-of-call summary, outcome, objections, next steps; live cockpit (agent reasoning, tools, sources, latency); sales dashboard behind the admin login |

**Architecture.** Browser ↔ FastAPI ↔ Agent (think → act → observe, max 3 tools) ↔ RAG / Tools / LLM gateway (automatic fallback across 5 models on Groq + Gemini) ↔ CRM adapter. Diagrams and the full end-to-end sequence: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

**Technical feasibility.** Every external dependency sits behind one swappable module (`llm.py`, `crm.py`, `tts.py`), and every layer has a fallback, so a live call never goes silent. The LLM *suggests*, code *decides*: tools validate arguments, and the lead score is deterministic. Production path: telephony media streams (Exotel/Twilio), streaming STT/TTS, Redis sessions, PostgreSQL + pgvector, worker queue. The agent logic stays unchanged.

**Commercial feasibility.** Estimated cost **≈ ₹10 per 4-minute call** (LLM ₹1.7, STT ₹2.5, TTS ₹2.1, telephony ₹3, infra ₹0.5). The sample Growth plan (₹45,000 for 2,000 calls ≈ ₹22.5/call) gives **~55% gross margin**, and costs the client less per call than a human executive (~₹30) while running 24x7 in 5 languages. It fits Fortivex's ecosystem as an add-on to **Fortivex CRM** and a vertical for **Medivex AI** (clinics) and **Empowers Academy** (admissions).

**Scaling to 1,00,000 users.** 1 lakh calls/month ≈ **40 concurrent calls** at peak (small cluster, ~₹11.5 lakh/month total). 1 lakh calls/day ≈ **800 concurrent** → ~12 stateless autoscaled pods, Redis sessions, managed Postgres, queue-based analysis workers, provisioned LLM throughput with multi-provider failover. Target p95 response < 1.2 s via token-to-TTS streaming. Details: [`docs/SCALING_SECURITY_COSTS.md`](docs/SCALING_SECURITY_COSTS.md).

**Security & compliance.** Staff sign in with Google (OAuth + PKCE) and get one of 5 roles (admin, manager, sales_rep, viewer, customer) checked on every route and tool, with an audit log; a break-glass admin password (login rate limit) can be turned off; customer calls run in customer mode and never receive internal data; input/output guardrails (card/Aadhaar/PAN/OTP masking, prompt-injection detection, no invented prices or bookings); knowledge split into **public** (agent may say it) and **internal** (admin-only, never in the agent's prompt, so it can't leak); secrets out of code; Pydantic validation and length limits; parameterised SQL with allow-listed columns; HTML escaping (no XSS); tool allow-list against prompt injection; grounding threshold against hallucination. Production: TLS, tokens and sessions in Redis, India data residency, DPDP Act 2023 consent, AI/recording disclosure, TRAI DND rules for outbound.

**Limitations & next steps.** MVP is single tenant on SQLite, keeps call sessions and login tokens in memory (lost on restart), runs on free-tier LLM/speech quotas and takes ~2-4 s per turn with the natural voice (no streaming yet). Next: real phone number via Exotel, streaming pipeline, WhatsApp confirmations, Fortivex CRM adapter, growing the 22 golden-conversation evals into a set of ~100 real customer questions.
