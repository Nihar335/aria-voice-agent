# Aria: interview demo run-sheet

## 1. The night before (5 min)
- Restart the laptop (this clears any old Python server still holding port 8000).
- Plug in the charger, connect to reliable Wi-Fi. Aria needs internet for the LLMs (Groq/Gemini) and the voices.
- Do one full practice call using the steps below.

## 2. Start the server (in front of the CEO)
Open **PowerShell** and run these two commands:

```powershell
cd "C:\Projects of Nihar\Fortivex Ai\aria-voice-agent"
```

```powershell
.venv\Scripts\python -m uvicorn main:app --port 8000
```

Wait until you see:
```
Application startup complete.
Uvicorn running on http://127.0.0.1:8000
[rag:public] 24 chunks loaded, search mode = embeddings
```
Leave this window open. It is the server, and its log is a nice thing to show too.
To stop it: click in the window and press **Ctrl + C**.

## 3. Pages to open in Chrome
Speech-to-text runs on the server (Whisper / Gemini); Chrome's own recognizer is only the last backup.

Open **`docs/architecture.html`** in a second tab first: the architecture diagram, the end-to-end workflow, the business case, scaling, security and costs on one page. Present from it.

| URL | What to show |
|---|---|
| http://localhost:8000 | One page. It asks **I'm a customer** or **I'm an admin** (⇄ Switch role in the top right) |
| → I'm a customer | The call plus a **live chat summary** on the right. No internal data |
| → I'm an admin | **Sign in with Google** (or the `ADMIN_PASSWORD` from `.env`) → **Live call** (call + cockpit: lead score, BANT, reasoning, latency), Knowledge, Sales dashboard, **Quality** |
| http://localhost:8000/docs | API docs (FastAPI) |

Press **Start call** and click **Allow** when Chrome asks for the microphone.

## 4. Script to say during the call
1. "Hi, I run a dental clinic in Jayanagar. We miss a lot of calls after 7 pm."
2. "How much would this cost?" → price from the documents, with the source shown
3. "Thoda mehenga lag raha hai, discount milega?" → Hinglish + objection handling
4. "Okay, book a demo tomorrow at 3." → Aria first asks every booking question, one at a time: name, business, problem, budget, decision maker, start date, email. Answer each; then she checks the slot and books it
5. "What's the capital of France?" → it refuses to guess
6. End the call → final summary in the panel. Then ⇄ Switch role → Admin → Sales dashboard: the lead is there.
7. Call again with the same phone number → "welcome back" (memory).

## 5. Extra things to show if asked
Open a **second** PowerShell window (keep the server running in the first), then `cd` into the project as above:

```powershell
.venv\Scripts\python -m unittest discover tests
```
Offline tests (guardrails, router, RBAC). No internet needed.

```powershell
.venv\Scripts\python evals/run.py
```
22 golden conversations scored against the real models (takes a few minutes, uses API quota).

```powershell
.venv\Scripts\python agent.py
```
Text chat with Aria in the terminal. **Use this as a backup** if the microphone or speakers fail.

## 6. If something goes wrong
| Problem | Fix |
|---|---|
| `error while attempting to bind on address ... 8000` | Another server is already running. Run `Get-Process python \| Stop-Process` then start again, or use `--port 8001` and open http://localhost:8001 |
| Mic does nothing | Use Chrome, click the lock icon in the address bar → Microphone → Allow, reload. The 🎙️ ring grows when it hears you |
| Words come out wrong | Pick the language you speak in the menu before starting. Bluetooth earbuds switch to low-quality "call" audio while the mic is on: wired earphones or the laptop mic are clearer |
| Pick the language before **Start call** | Kannada/Telugu are understood best when chosen in the menu first; Aria keeps answering in that language |
| Slow or "rate limit" replies | It falls back to the other provider automatically; wait a few seconds. Groq/Gemini free tiers have per-minute limits, so don't run the evals right before the call demo |
| No voice heard | Check the laptop volume / output device |
| Wi-Fi drops | Show the offline tests (section 5) and the architecture in `docs/architecture.html` |

## 7. Live coding, debugging and real-time changes
Run the server with `--reload` so every save restarts it:

```powershell
.venv\Scripts\python -m uvicorn main:app --port 8000 --reload
```

**Where each likely request goes** (one file and one test each):
| If asked to… | Change | Check with |
|---|---|---|
| Change a price / add a service | Upload the document in Admin → Knowledge (the cache expires old answers by itself) | Ask the price on a call; the source is shown |
| Add a tool, e.g. cancel a demo | `tools.py` (function + args class + `REGISTRY`), `CALL_TOOLS` in `layers/rbac.py`, a method in `crm.py` | `tests/test_api_rbac.py` |
| Add a language, e.g. Tamil | `LANGUAGE_NAMES` in `agent.py`, `VOICES` in `tts.py`, the STT rule in `stt.py`, the menu in `frontend/index.html` | `tests/test_voice.py`, a live call |
| Add or tune a guardrail | `check_input` / `check_output` in `layers/guardrails.py` | `tests/test_layers.py`, Quality tab → guard hits |
| Change Aria's behaviour or tone | `prompts/aria_system.md` (new version hash appears in every trace) | `python evals/run.py` |
| Switch model / provider | `.env`: `LLM_PROVIDER`, `GROQ_MODEL`, `GEMINI_MODEL`, fast-tier models | Quality tab: model, tokens, cost per turn |
| Connect a real CRM | Fill in `FortivexCRMAdapter` in `crm.py` | Book a demo; it appears in the CRM |

**Debugging a bad answer, in this order:**
1. Admin → **Quality** → open that turn's trace: facts used, route, model, prompt version, tool calls and results, guardrail hits, time per step.
2. Server log tags show every fallback: `[rag]`, `[llm]`, `[stt]`, `[tts]`.
3. http://localhost:8000/api/health shows keys, RAG mode, cache stats and which models are cooling down after a rate limit.
4. Reproduce without voice: `.venv\Scripts\python agent.py`. Each module also runs alone: `python rag.py`, `python llm.py`, `python tts.py`.
5. After the fix: `.venv\Scripts\python -m unittest discover tests` (49 tests, offline, ~3 s).
