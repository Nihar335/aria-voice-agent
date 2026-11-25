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
| 📝 Call summary, next steps & insights dashboard | `analysis.py`, `frontend/index.html` |
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
6. End the call → summary → dashboard. Call again with the same number → "welcome back".

## Project structure
```
main.py          FastAPI server: API endpoints + serves the web page
agent.py         Agent brain: system prompt, think → act → observe loop, call sessions
llm.py           LLM gateway: Gemini / Groq, automatic fallback, JSON mode, latency
rag.py           Retrieval: chunking, embeddings, similarity search, keyword fallback
tools.py         Actions the agent can take (with validation)
crm.py           CRM adapter (SQLite today; Fortivex CRM / Zoho / HubSpot = one class)
analysis.py      Language detection, sentiment, BANT lead score, call summary
tts.py           Neural voices (edge-tts) per language
config.py        Settings from .env
knowledge_base/  Company documents the agent answers from (sample pricing)
frontend/        Call screen + live cockpit + insights dashboard (one HTML file)
docs/            Architecture, scaling, security, costs
```

## Configuration (`.env`)
| Variable | Meaning |
|---|---|
| `GEMINI_API_KEY`, `GROQ_API_KEY` | LLM keys (at least one) |
| `LLM_PROVIDER` | `groq` or `gemini`: which to try first; the other is the backup |
| `GROQ_MODEL`, `GEMINI_MODEL` | Comma-separated model lists, tried in order (each model has its own rate limit) |
| `GEMINI_EMBED_MODEL` | Embedding model for RAG |

Each module can be tested alone: `python llm.py`, `python rag.py`, `python analysis.py`, `python tts.py`, and `python agent.py` (text chat in the terminal).

> Prices in `knowledge_base/` are **sample prices for this demo**, not official Fortivex AI pricing.
