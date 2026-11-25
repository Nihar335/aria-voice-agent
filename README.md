# Aria: Autonomous AI Sales Voice Agent

A voice sales agent that talks to customers in **English, Hindi, Hinglish, Kannada and Telugu**, answers only from company documents, qualifies leads, books demos into a CRM, and shows its reasoning live. Built from scratch in Python (no no-code tools).

📄 **[1-page report](REPORT.md)** · 🏗️ **[Architecture + end-to-end workflow](docs/ARCHITECTURE.md)** · 📈 **[Scaling, security, costs](docs/SCALING_SECURITY_COSTS.md)**

## Features
| Feature | Where in the code |
|---|---|
| 🎙️ Real-time voice: speech-to-text, neural text-to-speech, barge-in | `frontend/index.html`, `tts.py` |
| 🧠 Memory (in-call + returning caller) & 🌐 multilingual with auto-switching | `agent.py`, `analysis.py` |
| 🎯 Lead qualification (BANT score 0-100) & 😊 sentiment with escalation alert | `analysis.py` |
| 📚 RAG with sources and a "no guessing" threshold | `rag.py`, `knowledge_base/` |
| 📅 Appointment booking (validated slots + Google Calendar) & CRM adapter | `tools.py`, `crm.py` |
| 📝 Call summary, next steps & sales dashboard (admin login) | `analysis.py`, `admin.py`, `frontend/admin.html` |
| 🔐 Knowledge admin: upload PDF/MD/TXT, public vs confidential collections | `admin.py`, `rag.py`, `frontend/admin.html` |
| ⚡ Live cockpit: agent thoughts, tool calls, sources, latency per stage | `frontend/index.html` |
| 🙋 Human handoff, 🔁 LLM fallback chain (Groq ⇄ Gemini, 5 models) | `tools.py`, `llm.py` |

## Quick start (Windows)
Requirements: Python 3.11+, Google Chrome, free API keys from [Google AI Studio](https://aistudio.google.com/apikey) and [Groq](https://console.groq.com/keys).
```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env          # then paste your keys into .env
.venv\Scripts\python -m uvicorn main:app --reload
```
Open **http://localhost:8000** in Chrome, press **Start call**, allow the microphone and talk. API docs: http://localhost:8000/docs.

Docker: `docker build -t aria .` then `docker run -p 8000:8000 --env-file .env aria`.

## Try this conversation
1. "Hi, I run a dental clinic in Jayanagar. We miss a lot of calls after 7 pm."
2. "How much would this cost?" (price from the documents, with source)
3. "Thoda mehenga lag raha hai, discount milega?" (Hinglish + objection)
4. "Okay, book a demo tomorrow at 3." (availability check + booking + calendar link)
5. "What's the capital of France?" (it will not guess)
6. End the call → summary. Open `/admin.html` → sales dashboard. Call again with the same number → "welcome back".

## Project structure
```
main.py          FastAPI server: API endpoints + serves the web page
admin.py         Admin API: login, sales dashboard, knowledge upload/delete/search
agent.py         Agent brain: system prompt, think → act → observe loop, call sessions
llm.py           LLM gateway: Gemini / Groq, automatic fallback, JSON mode, latency
rag.py           Retrieval: public + internal collections, chunking, embeddings, keyword fallback
tools.py         Actions the agent can take (with validation)
crm.py           CRM adapter (SQLite today; Fortivex CRM / Zoho / HubSpot = one class)
analysis.py      Language detection, sentiment, BANT lead score, call summary
tts.py           Neural voices (edge-tts) per language
config.py        Settings from .env
knowledge_base/  Public company documents the agent answers from (sample pricing)
knowledge_internal/ Confidential documents, admin search only (git-ignored)
frontend/        index.html: call screen + live cockpit · admin.html: login, dashboard, knowledge
docs/            Architecture, scaling, security, costs
```

## Knowledge admin (upload files)

Open `http://localhost:8000/admin.html` and log in with `ADMIN_PASSWORD` from `.env`.
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

Uploads default to **Internal**. The client page (`index.html`) has no admin access: every
`/api/admin/...` route needs the login token, and neither folder is served as static files.

## Configuration (`.env`)
| Variable | Meaning |
|---|---|
| `GEMINI_API_KEY`, `GROQ_API_KEY` | LLM keys (at least one) |
| `LLM_PROVIDER` | `groq` or `gemini`: which to try first; the other is the backup |
| `GROQ_MODEL`, `GEMINI_MODEL` | Comma-separated model lists, tried in order (each model has its own rate limit) |
| `GEMINI_EMBED_MODEL` | Embedding model for RAG |
| `ADMIN_PASSWORD` | Password for `/admin.html` (empty = admin page disabled) |

Each module can be tested alone: `python llm.py`, `python rag.py`, `python analysis.py`, `python tts.py`, and `python agent.py` (text chat in the terminal).

> Prices in `knowledge_base/` are **sample prices for this demo**, not official Fortivex AI pricing.
