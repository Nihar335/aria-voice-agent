# Architecture

## 0. The 7-layer stack (one customer turn)

```mermaid
flowchart TB
    IN([Customer said something]) --> G1
    subgraph L6a["6 · Guardrails (input) - layers/guardrails.py"]
        G1["rate limit · mask card/Aadhaar/PAN/OTP<br/>prompt injection · abuse → handoff"]
    end
    G1 -->|blocked| OUT
    G1 --> R["3 · RAG - rag.py<br/>company facts + sources"]
    R --> RT
    subgraph L7["7 · Cache + router - layers/router.py, layers/cache.py"]
        RT["router: faq / chat → fast model<br/>action / sensitive / kn / te → strong model"]
        C{"FAQ answer<br/>cached?"}
        RT --> C
    end
    C -->|hit, 0 LLM calls| OUT
    C -->|miss| P["2 · System prompt - prompts/aria_system.md<br/>versioned (hash in every trace)"]
    P --> LLM["1 · LLM call - llm.py<br/>Groq ⇄ Gemini fallback, tokens, latency"]
    LLM <-->|tool call / result| T["4 · Tools - tools.py (JSON schema + validation)<br/>also served over MCP - mcp_server.py"]
    T --- RB["5 · RBAC - layers/rbac.py<br/>customer role: 5 call tools only<br/>staff: Google sign-in + roles + audit log"]
    LLM --> G2
    subgraph L6b["6 · Guardrails (output)"]
        G2["price / promise not in facts · fake booking · prompt leak<br/>→ regenerate once → safe fallback"]
    end
    G2 --> OUT([Reply spoken to customer])
    OUT -.-> OBS["8 · Observability - layers/observability.py<br/>1 trace per turn: spans, tokens, cost, guard hits<br/>+ evals/run.py golden conversations"]
```

| Layer | Fails safe how |
|---|---|
| Guardrails | Code, not prompt: a regex/number check can't be talked out of its rule. One regeneration, then a fixed safe reply |
| Router | Fast tier falls back to the strong list; a tool call switches the rest of the turn to the strong model; empty reply → retry on strong |
| Cache | Key includes knowledge-base hash + prompt version, so new prices or prompt edits invalidate it; only tool-free, guard-clean, impersonal answers are stored |
| RBAC | The page hides what you can't use, but every route and every tool call is checked on the server; role changes apply immediately |
| Observability | A trace write can never break a call (errors are logged, not raised) |

## 1. MVP architecture (what runs today)

```mermaid
flowchart TB
    subgraph BROWSER["Browser · frontend/"]
        MIC["Mic + speaker<br/><small>call-core.js · barge-in</small>"]
        CALL["Call screen<br/><small>customer summary · admin cockpit</small>"]
        ADM["Admin tabs<br/><small>knowledge · sales · quality · users</small>"]
    end

    subgraph SERVER["FastAPI server · main.py"]
        API["Call API<br/><small>start · stt · turn · live · end</small>"]
        ADMIN["Admin API + RBAC<br/><small>admin.py · layers/rbac.py</small>"]
        STT["Speech-to-text<br/><small>stt.py</small>"]
        AGENT["Agent loop<br/><small>agent.py · 7-layer pipeline · max 3 tools</small>"]
        TTS["Text-to-speech<br/><small>tts.py</small>"]
        ANA["Analysis<br/><small>analysis.py · BANT · summary</small>"]
        RAG["RAG<br/><small>rag.py · sources + threshold</small>"]
        TOOLS["Tools<br/><small>tools.py · booking · handoff</small>"]
        LLM["LLM gateway<br/><small>llm.py · 5-model fallback</small>"]
    end

    subgraph AI["AI providers"]
        GROQ["Groq<br/><small>Qwen · GPT-OSS · Whisper</small>"]
        GEM["Google Gemini<br/><small>LLM · embeddings · STT · TTS</small>"]
        EDGE["edge-tts<br/><small>neural Indian voices</small>"]
    end

    subgraph DATA["Data"]
        KB[("knowledge_base/<br/><small>public</small>")]
        CRM[("CRM · SQLite<br/><small>leads · calls · demos</small>")]
        KBI[("knowledge_internal/<br/><small>admin only</small>")]
    end

    MIC -->|audio| API
    CALL -->|turn · live| API
    ADM -->|token| ADMIN

    API --> STT
    API --> AGENT
    API --> TTS
    API -.->|after reply| ANA

    AGENT --> RAG
    AGENT --> TOOLS
    AGENT --> LLM
    ANA --> LLM

    STT --> GROQ
    STT -.-> GEM
    LLM --> GROQ
    LLM -.->|fallback| GEM
    TTS --> EDGE
    TTS -.->|Telugu| GEM

    RAG --> KB
    TOOLS --> CRM
    ANA --> CRM
    ADMIN --> CRM
    ADMIN --> KBI

    classDef browser fill:#E6F4F2,stroke:#0F766E,color:#0B3D38
    classDef server fill:#EEF3F8,stroke:#1E3A5F,color:#14263D
    classDef ai fill:#FFF6E0,stroke:#B7791F,color:#4A3008
    classDef data fill:#F3F0FA,stroke:#5B4B8A,color:#2A2145
    class MIC,CALL,ADM browser
    class API,ADMIN,STT,AGENT,TTS,ANA,RAG,TOOLS,LLM server
    class GROQ,GEM,EDGE ai
    class KB,KBI,CRM data
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
    participant V as TTS (Gemini TTS / edge-tts)
    participant N as Analysis (background)

    C->>B: "Book a demo tomorrow at 3"
    B->>S: POST /api/call/stt (WAV audio)
    S-->>B: text (Groq Whisper; Gemini as backup)
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
| Audio in/out | Browser mic, server STT per utterance, Gemini TTS / edge-tts, HTTP per turn | Telephony WebSocket, streaming STT/TTS | new gateway service; agent unchanged |
| Sessions | Python dict | Redis | `SESSIONS` → Redis client |
| CRM | SQLite | PostgreSQL / Fortivex CRM API | one adapter class in `crm.py` |
| RAG index | NumPy in memory, public + internal collections | pgvector, one namespace per client and per collection | inside `rag.search()` only |
| Admin auth | Google sign-in + roles (RBAC), in-memory tokens | Company SSO, tokens in Redis | `_sessions` in `layers/rbac.py` |
| LLM | Free-tier keys | Vertex AI / paid tiers, quota pools | config only |
| Analysis | FastAPI background task | queue + worker pool | move function call to a worker |

Details: [SCALING_SECURITY_COSTS.md](SCALING_SECURITY_COSTS.md).
