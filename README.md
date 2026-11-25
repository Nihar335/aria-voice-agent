# Aria: Autonomous AI Sales Voice Agent

A voice sales agent that talks to customers in **English, Hindi, Hinglish, Kannada and Telugu**, answers only from company documents, qualifies leads, books demos into a CRM, and shows its reasoning live. Built from scratch in Python (no no-code tools).

📄 **[1-page report](REPORT.md)** · 🏗️ **[Architecture + end-to-end workflow](docs/ARCHITECTURE.md)** · 📈 **[Scaling, security, costs](docs/SCALING_SECURITY_COSTS.md)**

## Built in 7 layers (how companies build their own AI)
One API call is not a product. Every customer turn goes through these layers, in order ([layers/pipeline.py](layers/pipeline.py)):

| # | Layer | What it does here | Code |
|---|---|---|---|
| 1 | **LLM API call** | Groq ⇄ Gemini fallback chain, JSON mode, tokens + latency per call | `llm.py` |
| 2 | **System prompt** | Lives in its own file; every change gets a version hash recorded in traces and eval reports | `prompts/aria_system.md`, `layers/prompts.py` |
| 3 | **RAG** | Company facts with sources and a "no guessing" threshold; public vs confidential collections | `rag.py` |
| 4 | **MCP tools** | One tool registry with JSON schemas + validation, used in-process by the agent and served over MCP | `tools.py`, `mcp_server.py` |
| 5 | **RBAC + OAuth** | Google sign-in; roles admin / manager / sales_rep / viewer / customer checked on every route and tool; audit log | `layers/rbac.py` |
| 6 | **Guardrails** | In: rate limits, card/Aadhaar/PAN/OTP masking, prompt injection, abuse. Out: invented prices/promises/bookings → regenerate, voice format | `layers/guardrails.py` |
| 7 | **Cache + router** | FAQ turns → fast model, actions → strong model; repeated FAQ answers served from cache (0 LLM calls) | `layers/router.py`, `layers/cache.py` |
| 8 | **Observability + evals** | One trace per turn (spans, tokens, cost, guard hits) + 22 golden conversations scored by code and an LLM judge | `layers/observability.py`, `evals/` |

See it working: admin page → **Quality** tab (traces, eval score, guardrail hits, audit log).

```
.venv\Scripts\python -m unittest discover tests     # offline tests: guardrails, router, RBAC (no API keys)
.venv\Scripts\python evals/run.py                    # golden conversations against the real models
.venv\Scripts\python mcp_server.py                   # Aria's tools over MCP (Claude Desktop etc.)
```

## Features
| Feature | Where in the code |
|---|---|
| 🎙️ Real-time voice: server speech-to-text (Whisper / Gemini), natural voices (Gemini TTS for Telugu, edge-tts), barge-in | `frontend/call-core.js`, `stt.py`, `tts.py` |
| 🧠 Memory (in-call + returning caller) & 🌐 multilingual with auto-switching | `agent.py`, `analysis.py` |
| 🎯 Lead qualification (BANT score 0-100) & 😊 sentiment with escalation alert | `analysis.py` |
| 📚 RAG with sources and a "no guessing" threshold | `rag.py`, `knowledge_base/` |
| 📅 Appointment booking (validated slots + Google Calendar) & CRM adapter | `tools.py`, `crm.py` |
| 📝 Live chat summary for the customer, call summary, next steps & sales dashboard (admin login) | `analysis.py`, `admin.py`, `frontend/customer.js`, `frontend/admin.js` |
| 🔐 Knowledge admin: upload PDF/MD/TXT, public vs confidential collections | `admin.py`, `rag.py`, `frontend/admin.js` |
| ⚡ Live cockpit: agent thoughts, tool calls, sources, latency per stage | `frontend/cockpit.js` |
| 🙋 Human handoff, 🔁 LLM fallback chain (Groq ⇄ Gemini, 5 models) | `tools.py`, `llm.py` |

## Quick start (Windows)
Requirements: Python 3.11+, Google Chrome, free API keys from [Google AI Studio](https://aistudio.google.com/apikey) and [Groq](https://console.groq.com/keys).
```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env          # then paste your keys into .env
.venv\Scripts\python -m uvicorn main:app --reload
```
Open **http://localhost:8000** in Chrome. One page asks **"I'm a customer"** or **"I'm an admin"**:
- **Customer** (`/#customer`): the call plus a **live chat summary** that builds up while you talk. The server runs these calls in `customer` mode and never sends them internal data (score, reasoning, sources, objections).
- **Admin** (`/#admin`): asks for the password, then **Live call** (the same call plus the cockpit: lead score, BANT, agent reasoning, latency), **Knowledge**, **Sales dashboard**, **Quality** and **Users**.

Press **Start call**, allow the microphone and talk. API docs: http://localhost:8000/docs. The old `/call.html` and `/admin.html` links redirect to the right view.

Docker: `docker build -t aria .` then `docker run -p 8000:8000 --env-file .env aria`.

## Try this conversation
1. "Hi, I run a dental clinic in Jayanagar. We miss a lot of calls after 7 pm."
2. "How much would this cost?" (price from the documents, with source)
3. "Thoda mehenga lag raha hai, discount milega?" (Hinglish + objection)
4. "Okay, book a demo tomorrow at 3." (availability check + booking + calendar link)
5. "What's the capital of France?" (it will not guess)
6. End the call → summary. Switch role → Admin → sales dashboard. Call again with the same number → "welcome back".

## Project structure
```
main.py          FastAPI server: API endpoints + serves the web page
admin.py         Admin API: knowledge upload/delete/search, quality, traces, audit (RBAC per route)
agent.py         Call sessions + memory; each turn runs through layers/pipeline.py
layers/          The 7-layer stack: pipeline, prompts, guardrails, router, cache, rbac, observability
prompts/         The versioned system prompt
mcp_server.py    The same tools served over MCP
evals/           Golden conversations + runner (writes data/evals/report_*.json)
tests/           Offline unit tests (guardrails, router, RBAC, tool validation)
llm.py           LLM gateway: Gemini / Groq, automatic fallback, JSON mode, latency
rag.py           Retrieval: public + internal collections, chunking, embeddings, keyword fallback
tools.py         Tool registry: JSON schemas, validation, per-role permissions
crm.py           CRM adapter (SQLite today; Fortivex CRM / Zoho / HubSpot = one class)
analysis.py      Language detection, sentiment, BANT lead score, call summary
tts.py           Voices: Gemini TTS (Telugu, smoother accent) and edge-tts, with fallbacks
stt.py           Speech-to-text: Groq Whisper (English/Hindi) and Gemini (Telugu/Kannada/Hinglish)
config.py        Settings from .env
knowledge_base/  Public company documents the agent answers from (sample pricing)
knowledge_internal/ Confidential documents, admin search only (git-ignored)
frontend/        index.html: the single page (customer or admin) · call-core.js: the call (mic, voices, chat)
                 customer.js: live chat summary · cockpit.js: admin live call · admin.js: admin tabs · app.js: role router
                 my-calls.html: customer's private summary page
docs/            Architecture, scaling, security, costs
```

## Knowledge admin (upload files)

Open `http://localhost:8000`, choose **I'm an admin** and **Sign in with Google** (set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and your email in `ADMIN_EMAILS` in `.env`; the first sign-in makes you admin, then add teammates in the **Users** tab). `ADMIN_PASSWORD` still works as a break-glass admin login; leave it empty in production.
Upload `.pdf`, `.md` or `.txt` files into one of two collections:

| Collection | Folder | Who can read it |
|---|---|---|
| **Public** | `knowledge_base/` | The voice agent, so anything here may be said to customers |
| **Internal** | `knowledge_internal/` (git-ignored) | Admins only, via the admin search. The agent never loads it, so it can't leak it |

The admin page also has the **Sales dashboard** (leads, calls, booked demos), which is no
longer on the client page.

### Customer summary page

Every customer gets a private link, `/my-calls.html#<secret>`, shown in the end-of-call popup
and copyable from the admin dashboard (**Copy** / **Reset** per lead). It shows only that
customer's own calls: a summary written for them, what happens next, the transcript and any
booked demo. Internal notes (score, objections, sales next steps) are never sent to it.

Uploads default to **Internal**. The customer view has no admin access: every
`/api/admin/...` route needs the login token, and neither folder is served as static files.

## Configuration (`.env`)
| Variable | Meaning |
|---|---|
| `GEMINI_API_KEY`, `GROQ_API_KEY` | LLM keys (at least one) |
| `LLM_PROVIDER` | `groq` or `gemini`: which to try first; the other is the backup |
| `GROQ_MODEL`, `GEMINI_MODEL` | Comma-separated model lists, tried in order (each model has its own rate limit) |
| `GEMINI_EMBED_MODEL` | Embedding model for RAG |
| `ADMIN_PASSWORD` | Admin password on the main page (empty = password sign-in disabled) |
| `GEMINI_TTS_LANGS`, `GEMINI_TTS_VOICE` | Languages that use Gemini's natural voice (default `te`) and which voice (default `Sulafat`) |
| `STT_PROVIDER` | `auto` (Gemini for Telugu/Kannada/Hinglish, Groq Whisper otherwise), `groq` or `gemini` |
| `MAX_UPLOAD_MB` | Largest file the admin page accepts, in MB (default 50) |

Each module can be tested alone: `python llm.py`, `python rag.py`, `python analysis.py`, `python tts.py`, and `python agent.py` (text chat in the terminal).

> Prices in `knowledge_base/` are **sample prices for this demo**, not official Fortivex AI pricing.
