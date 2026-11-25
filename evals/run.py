"""
evals/run.py - LAYER 8 (part 2): measure quality after EVERY change.

    .venv\\Scripts\\python evals/run.py                 # all cases
    .venv\\Scripts\\python evals/run.py --only price    # cases whose id contains "price"
    .venv\\Scripts\\python evals/run.py --min 0.85      # exit code 1 if pass rate < 85 % (use in CI)

How it works
  - Each case in evals/cases.json is a short conversation with checks on Aria's last reply.
  - Runs against a THROW-AWAY CRM + trace database (data/evals/), so real leads and demo
    slots are never touched. The answer cache is off, so every case really hits the model.
  - Two kinds of checks:
      code checks  -> exact and free: tool called, booked, handoff, language, script, words, route...
      LLM judge    -> for meaning: "is every fact in the reply supported by the facts?",
                      "did she avoid promising a discount?" (runs on Gemini, so Groq limits stay free)
  - Writes data/evals/report_<time>.json with the prompt version, so the admin Quality tab
    can show the score over time and which prompt version produced it.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "evals"
OUT.mkdir(parents=True, exist_ok=True)

# Must be set BEFORE the app modules read config.
for name in ("eval_crm.db", "eval_obs.db"):
    (OUT / name).unlink(missing_ok=True)
os.environ["CRM_DB_PATH"] = str(OUT / "eval_crm.db")
os.environ["OBS_DB_PATH"] = str(OUT / "eval_obs.db")
os.environ["CACHE_ENABLED"] = "false"
os.environ["MAX_TURNS_PER_MINUTE"] = "1000"
sys.path.insert(0, str(ROOT))

import llm                                     # noqa: E402
from agent import Session, greeting, handle_turn, remember_reply   # noqa: E402
from crm import get_crm                        # noqa: E402
from layers import prompts                     # noqa: E402
from rag import format_context, get_kb         # noqa: E402

SCRIPTS = {"hi": r"[ऀ-ॿ]", "kn": r"[ಀ-೿]", "te": r"[ఀ-౿]"}

JUDGE_PROMPT = """You are a strict QA reviewer for a sales voice agent called Aria.
Decide if Aria's LAST REPLY passes the CHECK. Use the COMPANY FACTS as the only truth.
Reply ONLY as JSON: {"pass": true or false, "reason": "one short sentence"}"""

GROUNDED_CHECK = ("Every factual claim in the reply (prices, plans, numbers, timelines, features, policies) is "
                  "supported by the COMPANY FACTS. Saying a consultant will confirm, or asking a question, is fine. "
                  "Fail if any number or promise is not in the facts.")


def judge(check: str, conversation: str, facts: str) -> tuple[bool, str]:
    msg = f"COMPANY FACTS:\n{facts}\n\nCONVERSATION:\n{conversation}\n\nCHECK: {check}"
    try:
        data, _ = llm.chat_json(JUDGE_PROMPT, [{"role": "user", "content": msg}], temperature=0, prefer="gemini")
        return bool(data.get("pass")), str(data.get("reason", ""))[:200]
    except Exception as e:
        return False, f"judge error: {e}"


def run_case(case: dict, index: int) -> dict:
    session = Session(phone=f"90000{index:05d}")
    remember_reply(session, {"thought": "greeting", "tool": None, "args": {}, "reply": greeting(session),
                             "language": session.language})
    results, error = [], None
    t0 = time.perf_counter()
    try:
        for text in case["turns"]:
            results.append(handle_turn(session, text, case.get("language")))
    except Exception as e:
        error = repr(e)[:300]
    ms = int((time.perf_counter() - t0) * 1000)

    checks = []
    def check(name, ok, detail=""):
        checks.append({"check": name, "pass": bool(ok), "detail": detail})

    if error or not results:
        check("ran", False, error or "no turns")
        return {"id": case["id"], "category": case.get("category"), "pass": False, "checks": checks, "ms": ms}

    last = results[-1]
    reply = last["reply"]
    reply_all = f"{reply} || {last.get('reply_en', '')}".lower()
    events = [e for r in results for e in r["events"]]
    tools_called = [e["detail"] for e in events if e["type"] == "tool"]
    exp = case["expect"]

    if "tool_called" in exp:
        check("tool_called", exp["tool_called"] in tools_called, f"tools: {tools_called}")
    if "booked" in exp:
        booked = bool(get_crm().appointments_for_lead(session.lead_id))
        check("booked", booked == exp["booked"], f"booked={booked}")
    if "handoff" in exp:
        check("handoff", session.handoff == exp["handoff"], f"handoff={session.handoff}")
    if "language" in exp:
        check("language", last["language"] == exp["language"], f"got {last['language']}")
    if "script" in exp:
        check("script", re.search(SCRIPTS[exp["script"]], reply) is not None, reply[:60])
    if "max_words" in exp:
        n = len(reply.split())
        check("max_words", n <= exp["max_words"], f"{n} words")
    if "contains_any" in exp:
        check("contains_any", any(s.lower() in reply_all for s in exp["contains_any"]), reply[:100])
    if "not_contains" in exp:
        hit = [s for s in exp["not_contains"] if s.lower() in reply_all]
        check("not_contains", not hit, f"found {hit}" if hit else "")
    if "transcript_not_contains" in exp:
        text = json.dumps(session.transcript + session.history, ensure_ascii=False)
        hit = [s for s in exp["transcript_not_contains"] if s in text]
        check("transcript_not_contains", not hit, f"found {hit}" if hit else "masked")
    if "guard_input" in exp:
        flags = [g for r in results for g in r.get("guard", [])]
        check("guard_input", exp["guard_input"] in flags, f"flags: {flags}")
    if "route" in exp:
        check("route", last.get("route") == exp["route"], f"got {last.get('route')}")
    if "lead_field" in exp:
        lead = get_crm().get_lead(session.lead_id)
        for k, v in exp["lead_field"].items():
            check(f"lead.{k}", v.lower() in str(lead.get(k) or "").lower(), f"{k}={lead.get(k)}")

    conversation = "\n".join(f"{'Aria' if t['who'] == 'agent' else 'Customer'}: {t['text']}" for t in session.transcript)
    facts = format_context(get_kb().search(case["turns"][-1], k=4))
    if exp.get("grounded"):
        ok, why = judge(GROUNDED_CHECK, conversation, facts)
        check("grounded (judge)", ok, why)
    if "judge" in exp:
        ok, why = judge(exp["judge"], conversation, facts)
        check("judge", ok, why)

    return {"id": case["id"], "category": case.get("category"), "pass": all(c["pass"] for c in checks),
            "checks": checks, "ms": ms, "reply": reply, "reply_en": last.get("reply_en"),
            "route": last.get("route"), "provider": last.get("provider"),
            "tokens": sum(r.get("tokens", {}).get("in", 0) + r.get("tokens", {}).get("out", 0) for r in results)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="run only cases whose id contains this text")
    ap.add_argument("--min", type=float, default=0.0, help="fail (exit 1) if pass rate is below this")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between cases (free-tier rate limits)")
    args = ap.parse_args()

    cases = json.loads((Path(__file__).parent / "cases.json").read_text(encoding="utf-8"))["cases"]
    cases = [c for c in cases if args.only in c["id"]]
    print(f"Running {len(cases)} cases · prompt {prompts.version()}\n")

    results = []
    for i, case in enumerate(cases):
        r = run_case(case, i)
        if any("All LLM providers failed" in c["detail"] for c in r["checks"]):
            # Free-tier rate limit (HTTP 429): that's capacity, not quality. Wait and run the case once more.
            print(f"      rate-limited on {case['id']}, waiting 30 s and retrying...")
            time.sleep(30)
            r = run_case(case, i + 1000)
        results.append(r)
        mark = "PASS" if r["pass"] else "FAIL"
        print(f"{mark}  {case['id']:<26} {r['ms']:>6} ms  {r.get('route') or '':<9} {(r.get('reply') or '')[:70]}")
        for c in r["checks"]:
            if not c["pass"]:
                print(f"        x {c['check']}: {c['detail']}")
        time.sleep(args.delay)

    passed = sum(r["pass"] for r in results)
    ms = sorted(r["ms"] for r in results)
    by_cat: dict[str, list] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r["pass"])
    summary = {
        "date": time.strftime("%Y-%m-%d %H:%M:%S"), "prompt_version": prompts.version(),
        "total": len(results), "passed": passed,
        "pass_rate": round(passed / len(results), 3) if results else 0,
        "avg_case_ms": round(sum(ms) / len(ms)) if ms else 0,
        "p95_case_ms": ms[min(len(ms) - 1, int(0.95 * (len(ms) - 1) + 0.5))] if ms else 0,
        "tokens": sum(r.get("tokens", 0) for r in results),
        "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_cat.items())},
    }
    report = OUT / f"report_{time.strftime('%Y%m%d_%H%M%S')}.json"
    report.write_text(json.dumps({"summary": summary, "cases": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{passed}/{len(results)} passed ({summary['pass_rate']:.0%}) · by category {summary['by_category']}")
    print(f"Report: {report.relative_to(ROOT)}")
    sys.exit(1 if summary["pass_rate"] < args.min else 0)


if __name__ == "__main__":
    main()
