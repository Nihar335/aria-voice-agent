# Aria: interview demo run-sheet

## 1. The night before (5 min)
- Restart the laptop (this clears any old Python server still holding port 8000).
- Plug in the charger, connect to reliable Wi-Fi. Aria needs internet for the LLMs (Groq/Gemini) and the voices.
- Do one full practice call using the steps below.

## 2. Start the server (in front of the CEO)
Open **PowerShell** and run these two commands:

```powershell
cd "C:\Projects of Nihar\Fortivex Ai"
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

## 3. Pages to open in Chrome (use Chrome, it has the speech recognition)
| URL | What to show |
|---|---|
| http://localhost:8000 | One page. It asks **I'm a customer** or **I'm an admin** (⇄ Switch role in the top right) |
| → I'm a customer | The call plus a **live chat summary** on the right. No internal data |
| → I'm an admin | Password (`ADMIN_PASSWORD` from `.env`) → **Live call** (call + cockpit: lead score, BANT, reasoning, latency), Knowledge, Sales dashboard, **Quality** |
| http://localhost:8000/docs | API docs (FastAPI) |

Press **Start call** and click **Allow** when Chrome asks for the microphone.

## 4. Script to say during the call
1. "Hi, I run a dental clinic in Jayanagar. We miss a lot of calls after 7 pm."
2. "How much would this cost?" → price from the documents, with the source shown
3. "Thoda mehenga lag raha hai, discount milega?" → Hinglish + objection handling
4. "Okay, book a demo tomorrow at 3." → availability check + booking
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
| Telugu voice sounds like the old robotic one | Gemini's free voice quota (3 per minute per model) ran out for a moment; it uses the backup voice and comes back by itself |
| Slow or "rate limit" replies | It falls back to the other provider automatically; wait a few seconds. Groq/Gemini free tiers have per-minute limits, so don't run the evals right before the call demo |
| No voice heard | Check the laptop volume / output device |
| Wi-Fi drops | Show the offline tests (section 5) and the architecture in `docs/ARCHITECTURE.md` |
