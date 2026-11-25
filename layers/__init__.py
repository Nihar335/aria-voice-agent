"""
layers/ - Aria built the way companies build their own AI: one API call -> 7 layers.

Every customer turn passes through the layers in this order (layers/pipeline.py):

    customer text
        |
    [6] GUARDRAILS (input)   layers/guardrails.py   rate limit, prompt injection, abuse, mask card/Aadhaar/PAN/OTP
        |
    [3] RAG                  rag.py                 find company facts for this question
        |
    [7] ROUTER + CACHE       layers/router.py       simple turn -> fast model, action turn -> strong model
                             layers/cache.py        same FAQ question again -> answer from memory (0 LLM calls)
        |
    [2] SYSTEM PROMPT        prompts/aria_system.md + layers/prompts.py (versioned, the version goes in every trace)
        |
    [1] LLM API CALL         llm.py                 Groq <-> Gemini fallback chain, tokens + latency
        |
    [4] MCP TOOLS            tools.py               one tool registry with JSON schemas + validation
                             mcp_server.py          the same tools served over MCP (Claude Desktop, CRM, ...)
    [5] RBAC + OAUTH         layers/rbac.py         who may call which tool / admin route; Google sign-in
        |
    [6] GUARDRAILS (output)  layers/guardrails.py   prices/promises not in the facts, fake bookings, length, markdown
        |
    [8] OBSERVABILITY+EVALS  layers/observability.py  one trace per turn (spans, tokens, cost, guard hits)
                             evals/run.py             golden conversations scored after every change

Each layer is a small module with one job, so it can be read, tested and swapped alone.
"""
