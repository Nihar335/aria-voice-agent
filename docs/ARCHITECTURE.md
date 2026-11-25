# Architecture

## 1. MVP architecture (what runs today)

```mermaid
flowchart LR
    subgraph Browser["Browser - frontend/index.html"]
        MIC["🎙️ Chrome Speech-to-Text<br/>en-IN · hi-IN · kn-IN · te-IN"]
        UI["Call screen +<br/>Live cockpit<br/>(index.html)"]
        ADM["Admin page (admin.html)<br/>login · sales dashboard ·<br/>knowledge upload + search"]
        SPK["🔊 Audio player<br/>(browser voice fallback)"]
    end

    subgraph Server["FastAPI server - main.py"]
        API["REST API<br/>/api/call/start · turn · live · end<br/>/api/health"]
        ADMIN["Admin API - admin.py<br/>password login → token<br/>/api/admin/dashboard · documents · search"]
        AGENT["Agent brain - agent.py<br/>think → act → observe loop<br/>(max 3 tool steps)"]
        RAG["RAG - rag.py<br/>section chunks + Gemini embeddings<br/>TF-IDF offline fallback"]
        TOOLS["Tools - tools.py<br/>save_lead_info · check_availability<br/>book_appointment · handoff_to_human<br/>search_knowledge"]
        ANALYSIS["Analysis - analysis.py<br/>language · sentiment · BANT score<br/>call summary (background)"]
        TTS["TTS - tts.py<br/>edge-tts neural voices"]
        LLM["LLM gateway - llm.py<br/>5-model fallback chain<br/>fail-fast on rate limits<br/>JSON mode + latency"]
        CRM["CRM adapter - crm.py<br/>SQLiteCRM (today)<br/>FortivexCRMAdapter (plug-in)"]
    end

    KB[("knowledge_base/ (public)<br/>services · pricing · FAQ · objections")]
    KBI[("knowledge_internal/ (confidential)<br/>admin-only, never sent to the agent")]
    DB[("SQLite<br/>leads · calls · appointments")]
    GEM["Google Gemini API<br/>chat backup + embeddings"]
    GROQ["Groq API (primary)<br/>Qwen 3.8 · GPT-OSS 120B/20B"]
    MSV["Microsoft neural voices"]
    GCAL["Google Calendar<br/>pre-filled event link"]

    MIC -->|text| API
    API --> AGENT
    AGENT --> RAG
    RAG --> KB
    AGENT --> TOOLS
    TOOLS --> CRM
    TOOLS --> GCAL
    AGENT --> LLM
    ANALYSIS --> LLM
    LLM --> GROQ
    LLM -.->|fallback| GEM
    RAG -.->|embeddings| GEM
    API --> TTS
    TTS --> MSV
    API -->|reply + audio + sources + latency| SPK
    API --> ANALYSIS
    ANALYSIS --> CRM
    CRM --> DB
    UI -->|poll every 1.2 s| API
    ADM -->|Bearer token| ADMIN
    ADMIN -->|upload / delete → re-index| RAG
    ADMIN -->|leads · calls · demos| CRM
    RAG -->|admin search only| KBI
```

### Design principles
| Principle | Where |
|---|---|
| **One gateway per external dependency** so it can be swapped | `llm.py` (LLM), `crm.py` (CRM), `tts.py` (voice) |
| **Fallback at every layer**, so a live call never dies | Groq ⇄ Gemini model chain · slow embeddings → keyword search (circuit breaker) · neural voice → browser voice · LLM down → human handoff |
| **LLM suggests, code decides** | tools validate every argument; lead score is a formula, not an LLM guess |
| **Grounded answers only** | RAG with a minimum-similarity threshold; sources shown for every answer |
| **Nothing slow on the customer's path** | analysis and scoring run as background tasks after the reply is sent |

## 2. One full end-to-end workflow

```mermaid
sequenceDiagram
    autonumber
    actor C as Customer
    participant B as Browser
    participant S as FastAPI (main.py)
    participant A as Agent (agent.py)
    participant R as RAG (rag.py)
    participant L as LLM (Gemini / Groq)
    participant T as Tools + CRM
    participant V as TTS (edge-tts)
    participant N as Analysis (background)

    C->>B: "Book a demo tomorrow at 3"
    B->>B: Chrome speech-to-text (en-IN)
    B->>S: POST /api/call/turn {session_id, text}
    S->>A: handle_turn()
    A->>R: search(text)
    R-->>A: top chunks + sources (or "no info")
    A->>L: system prompt + facts + history (JSON mode)
    L-->>A: {"tool": "check_availability"}
    A->>T: check_availability()
    T-->>A: free slots
    A->>L: tool result
    L-->>A: {"tool": "book_appointment", "args": {"slot": "2026-11-27 15:00"}}
    A->>T: book_appointment() - validates slot, saves to CRM, builds calendar link
    T-->>A: booked ✔
    A->>L: tool result
    L-->>A: {"reply": "Done! Your demo is booked for...", "language": "en"}
    A-->>S: reply + sources + thoughts + tool trace + latency
    S->>V: speak(reply, "en")
    V-->>S: MP3 (base64)
    S-->>B: JSON response
    B->>C: 🔊 plays reply, shows booking banner
    S-)N: analyze_turn() after response
    N->>L: sentiment + BANT (JSON)
    N->>T: update lead score / status
    B->>S: GET /api/call/{id}/live (polling)
    S-->>B: score, sentiment, BANT, insights → cockpit
    Note over B,S: On "End call": POST /api/call/end → summary + next steps saved in CRM → sales dashboard (admin page).<br/>Next call from the same number loads this summary as memory.
```

## 3. Production architecture (for 1,00,000+ users)

```mermaid
flowchart LR
    PSTN["📞 Phone network<br/>Exotel / Twilio / Plivo"] -->|media stream<br/>WebSocket| GW
    WEB["Web / WhatsApp<br/>widget"] --> LB

    subgraph Cloud["Cloud - India region (Mumbai)"]
        LB["Load balancer + WAF<br/>TLS, rate limits"]
        GW["Voice gateway pods<br/>(autoscaled, stateless)<br/>VAD + barge-in"]
        AG["Agent service pods<br/>(autoscaled, stateless)"]
        Q[["Queue<br/>Redis Streams / SQS"]]
        WK["Workers<br/>analysis · summaries ·<br/>CRM sync · webhooks"]
        REDIS[("Redis<br/>live call sessions,<br/>LLM response cache")]
        PG[("PostgreSQL + pgvector<br/>multi-tenant CRM + RAG")]
        OBJ[("Object storage<br/>recordings, transcripts")]
        DASH["Admin + dashboard app<br/>(SSO, role-based access,<br/>knowledge management)"]
        OBS["Observability<br/>OpenTelemetry · Grafana ·<br/>LLM tracing · alerts"]
    end

    STT["Streaming STT<br/>Deepgram / Google / Sarvam"]
    LLMS["LLM pool<br/>Gemini (Vertex AI) primary<br/>Groq / others fallback"]
    TTSP["Streaming TTS<br/>Azure / Google / ElevenLabs"]
    EXT["Fortivex CRM · Zoho · HubSpot<br/>Google Calendar API · WhatsApp API"]

    LB --> GW
    LB --> DASH
    GW <--> STT
    GW <--> TTSP
    GW <--> AG
    AG <--> LLMS
    AG <--> REDIS
    AG <--> PG
    AG --> Q
    Q --> WK
    WK <--> LLMS
    WK --> PG
    WK --> EXT
    GW --> OBJ
    DASH --> PG
    AG -.-> OBS
    GW -.-> OBS
    WK -.-> OBS
```

What changes from MVP to production, and what does **not**:
| Layer | MVP | Production | Code impact |
|---|---|---|---|
| Audio in/out | Browser STT, edge-tts, HTTP per turn | Telephony WebSocket, streaming STT/TTS | new gateway service; agent unchanged |
| Sessions | Python dict | Redis | `SESSIONS` → Redis client |
| CRM | SQLite | PostgreSQL / Fortivex CRM API | one adapter class in `crm.py` |
| RAG index | NumPy in memory, public + internal collections | pgvector, one namespace per client and per collection | inside `rag.search()` only |
| Admin auth | One password, in-memory tokens | SSO + roles, tokens in Redis | `require_admin()` in `admin.py` |
| LLM | Free-tier keys | Vertex AI / paid tiers, quota pools | config only |
| Analysis | FastAPI background task | queue + worker pool | move function call to a worker |

Details: [SCALING_SECURITY_COSTS.md](SCALING_SECURITY_COSTS.md).
