"""
layers/pipeline.py - one customer turn through all the layers, in order.

    guard_in -> rag -> router -> cache? -> prompt -> LLM <-> tools -> guard_out -> cache.put -> trace

Each step is timed as a "span" in the turn's trace (layers/observability.py), so the admin
Quality tab can show exactly where the time and the tokens went.
"""
import json
from datetime import datetime

import llm
from analysis import detect_language, reply_language
from crm import get_crm
from layers import cache, guardrails, prompts, router
from layers.observability import Trace
from rag import format_context, get_kb
from tools import IST, run_tool, tool_descriptions

MAX_TOOL_STEPS = 3
CALLER_ROLE = "customer"      # RBAC: the voice agent acts with the customer's permissions


def run_turn(session, user_text: str, chosen_language: str | None = None) -> dict:
    trace = Trace(session.id, session.lead_id, "turn")
    session.sources, session.events = [], []
    try:
        result = _run(session, user_text, chosen_language, trace)
    except Exception as e:
        trace.set(error=repr(e)[:300])
        trace.save()
        raise
    trace.set(output=result["reply"], language=result["language"], sources=result["sources"])
    saved = trace.save()
    session.trace_ids.append(trace.id)
    result["trace_id"] = trace.id
    result["latency"]["total_ms"] = saved["total_ms"]
    result["tokens"] = {"in": saved["input_tokens"], "out": saved["output_tokens"], "cost_usd": saved["cost_usd"]}
    return result


def _result(session, reply, language, reply_en, customer_en, provider, thoughts, route, rag_ms, llm_ms,
            cache_hit=False, guard=None):
    from agent import remember_reply
    session.language = language
    remember_reply(session, {"thought": thoughts[-1] if thoughts else "", "tool": None, "args": {},
                             "reply": reply, "language": language,
                             "reply_en": reply_en, "customer_en": customer_en})
    return {
        "reply": reply, "language": language,
        "reply_en": reply_en or reply, "customer_en": customer_en,
        "sources": sorted(set(session.sources)), "thoughts": thoughts, "events": session.events,
        "handoff": session.handoff, "provider": provider, "route": route,
        "cache_hit": cache_hit, "guard": guard or [],
        "latency": {"rag_ms": rag_ms, "llm_ms": llm_ms},
    }


def clean_customer_en(candidate, customer_text: str) -> str:
    """The model's English version of what the customer said, unless it is really a tool result /
    system marker (that must never be shown as the customer's words)."""
    c = (candidate or "").strip() if isinstance(candidate, str) else ""
    if not c or c.startswith("[") or '"saved"' in c or '"booked"' in c:
        return customer_text
    return c


def _run(session, user_text: str, chosen_language: str | None, trace: Trace) -> dict:
    from agent import LANGUAGE_NAMES, rag_query

    trace.set(prompt_version=prompts.version())

    # ---- [6] input guardrails --------------------------------------------------------
    with trace.span("guard_in"):
        verdict = guardrails.check_input(user_text, session)
    text = verdict.text                       # sensitive numbers already masked
    trace.set(input=text)
    for flag in verdict.flags:
        trace.guard("input", flag, "blocked" if verdict.block_reply else "masked")
        session.events.append({"type": "guard", "detail": f"input: {flag}"})
    session.history.append({"role": "user", "content": text})
    session.transcript.append({"who": "customer", "text": text})

    if verdict.block_reply:
        if verdict.handoff:
            session.handoff = True
            get_crm().update_lead(session.lead_id, {"status": "needs_human"})
            session.events.append({"type": "handoff", "detail": "repeated abuse"})
        trace.set(route="guarded")
        return _result(session, verdict.block_reply, "en",
                       verdict.block_reply, text, "guardrail", ["input guardrail answered"], "guarded", 0, 0,
                       guard=verdict.flags)

    # ---- [3] RAG ---------------------------------------------------------------------
    kb = get_kb()
    with trace.span("rag") as sp:
        search_info = {}
        hits = kb.search(rag_query(session, text), info=search_info)
        sp.update(**search_info, hits=[{"source": h["source"], "score": h["score"]} for h in hits])
    rag_ms = trace.data["spans"][-1]["ms"]
    session.sources = [h["source"] for h in hits]
    session.events.append({"type": "rag", "detail": f"{len(hits)} facts found", "ms": rag_ms,
                           "sources": session.sources})

    # The language to answer in, decided by code from what the customer actually wrote
    # (the app's language menu follows Aria's last reply, so it alone would make one wrong switch stick).
    target_language = reply_language(text, chosen_language)
    trace.set(reply_language=target_language)

    # ---- [7] router ------------------------------------------------------------------
    with trace.span("router") as sp:
        r = router.route(text, session, bool(hits), target_language)
        sp.update(kind=r.kind, tier=r.tier, cacheable=r.cacheable, reason=r.reason)
    trace.set(route=r.kind)
    session.events.append({"type": "route", "detail": f"{r.kind} -> {r.tier} model ({r.reason})"})

    # ---- [2] prompt version (needed for the cache key) --------------------------------
    prompt_ver = prompts.version()
    trace.set(prompt_version=prompt_ver)

    # ---- [7] cache -------------------------------------------------------------------
    if r.cacheable:
        with trace.span("cache_get") as sp:
            hit = cache.get(text, target_language, kb, prompt_ver)
            sp.update(hit=bool(hit))
        if hit:
            trace.set(cache_hit=True)
            session.sources = hit["sources"]
            session.events.append({"type": "cache", "detail": f"answered from cache ({hit['match']})"})
            return _result(session, hit["reply"], hit["language"], hit["reply_en"], hit.get("customer_en") or text,
                           "cache", ["answered from cache"], r.kind, rag_ms, 0, cache_hit=True)

    # ---- [2] system prompt -----------------------------------------------------------
    facts = format_context(hits)
    system, _ = prompts.render(
        today=datetime.now(IST).strftime("%A, %d %B %Y, %I:%M %p"),
        memory=session.memory_text(),
        tools=tool_descriptions(CALLER_ROLE),
        context=facts,
        detected=detect_language(text),
        chosen=LANGUAGE_NAMES[target_language],
    )

    # ---- [1] LLM  <->  [4] tools (think -> act -> observe) ----------------------------
    llm_ms, provider, thoughts, tier = 0, "", [], r.tier
    tool_outputs, decision, retried, first_customer_en = [], {}, False, None
    for step in range(MAX_TOOL_STEPS + 1):
        with trace.span("llm", step=step, tier=tier):
            decision, res = llm.chat_json(system, session.history, temperature=0.3, tier=tier)
        trace.llm(res, purpose=f"step {step}", parsed=decision)
        llm_ms += res.latency_ms
        provider = res.provider
        if not decision.get("tool") and not (decision.get("reply") or "").strip() and not retried:
            # Small/backup models sometimes return JSON with an empty reply: retry once on the strong list.
            retried, tier = True, "strong"
            trace.guard("output", "empty_reply", "retried on strong model", res.provider)
            with trace.span("llm", step=f"{step} retry", tier=tier):
                decision, res = llm.chat_json(system, session.history, temperature=0.3, tier=tier)
            trace.llm(res, purpose="empty reply retry", parsed=decision)
            llm_ms += res.latency_ms
            provider = res.provider
        thoughts.append(decision.get("thought", ""))
        if step == 0:
            # Only the FIRST step translates what the customer said. After a tool call the last "user"
            # message is the tool result, and models then "translate" that JSON instead.
            first_customer_en = decision.get("customer_en")
        tool = decision.get("tool")
        if tool and step < MAX_TOOL_STEPS:
            args = decision.get("args") or {}
            with trace.span("tool", tool=tool) as sp:
                output = run_tool(session, tool, args, role=CALLER_ROLE)       # [5] RBAC checked inside
                sp.update(ok="error" not in output)
            trace.data["tools"].append({"tool": tool, "args": args, "result": output})
            tool_outputs.append(output)
            session.events.append({"type": "tool", "detail": tool, "args": args, "result": output})
            session.history.append({"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)})
            session.history.append({"role": "user", "content": f"[TOOL RESULT for {tool}] {json.dumps(output, ensure_ascii=False)}"})
            tier = "strong"        # a tool turn is an action turn: use the strong model from here on
            continue
        break

    reply = decision.get("reply") or "Sorry, could you say that again?"
    language = decision.get("language") or session.language
    reply_en = decision.get("reply_en") or ""
    customer_en = clean_customer_en(first_customer_en, text)

    # ---- [6] output guardrails (one regeneration, then a safe fixed reply) --------------
    tool_text = " ".join(json.dumps(o, ensure_ascii=False) for o in tool_outputs)
    booked_now = any(e["type"] == "booking" for e in session.events)
    has_booking = bool(get_crm().appointments_for_lead(session.lead_id))
    guard_rules = []
    for attempt in range(2):
        with trace.span("guard_out", attempt=attempt):
            v = guardrails.check_output(reply, facts=facts + " " + tool_text, customer_text=text,
                                        booked_now=booked_now, has_previous_booking=has_booking,
                                        language=target_language)
        reply = v.reply
        for rule, detail in v.notes:
            trace.guard("output", rule, "logged", detail)
        if attempt == 1 and all(rule in guardrails.SOFT_RULES for rule, _ in v.violations):
            for rule, detail in v.violations:
                trace.guard("output", rule, "kept after retry", detail)
            break
        if not v.violations:
            break
        guard_rules += [rule for rule, _ in v.violations]
        for rule, detail in v.violations:
            session.events.append({"type": "guard", "detail": f"output: {rule}"})
            trace.guard("output", rule, "regenerated" if attempt == 0 else "fallback", detail)
        if attempt == 0:
            reasons = "; ".join(f"{rule} ({detail})" for rule, detail in v.violations)
            fix = [{"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)},
                   {"role": "user", "content": (
                       f"[GUARDRAIL] Your last reply was blocked: {reasons}. Rewrite it using ONLY the COMPANY FACTS "
                       "and tool results. Do not state any price, guarantee or booking that is not in them. "
                       f"Write the reply in {LANGUAGE_NAMES[target_language]}. "
                       "Same JSON shape, tool must be null.")}]
            with trace.span("llm", step="regenerate", tier="strong"):
                decision, res = llm.chat_json(system, session.history + fix, temperature=0.2, tier="strong")
            trace.llm(res, purpose="guardrail regenerate", parsed=decision)
            llm_ms += res.latency_ms
            provider = res.provider
            reply = decision.get("reply") or reply
            language = decision.get("language") or language
            reply_en = decision.get("reply_en") or reply_en
        else:
            hard = next(rule for rule, _ in v.violations if rule not in guardrails.SOFT_RULES)
            reply = guardrails.SAFE_FALLBACK[hard]
            language, reply_en = "en", reply

    # ---- [7] cache.put: only clean, tool-free, impersonal FAQ answers -----------------
    name = (get_crm().get_lead(session.lead_id) or {}).get("name") or ""
    if r.cacheable and not tool_outputs and not guard_rules and not (name and name.lower() in reply.lower()):
        cache.put(text, target_language, kb, prompt_ver,
                  {"reply": reply, "language": language, "reply_en": reply_en, "customer_en": customer_en,
                   "sources": sorted(set(session.sources))})

    return _result(session, reply, language, reply_en, customer_en, provider, thoughts, r.kind, rag_ms, llm_ms,
                   guard=verdict.flags + guard_rules)
