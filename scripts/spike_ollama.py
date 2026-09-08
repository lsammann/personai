#!/usr/bin/env python3
"""Phase 0 spike: how fast and how well do local models classify email?

Answers two questions from docs/PLAN.md:
  1. Seconds per classification on this CPU -> backfill duration, model choice.
  2. Does Ollama's `format` schema actually constrain output, including
     emitting `reasoning` before `confidence` as DESIGN.md assumes?

Stdlib only - runs without a venv. Uses synthetic emails, not real mail.
"""

import json
import statistics
import time
import urllib.error
import urllib.request

OLLAMA = "http://localhost:11434/api/chat"
MODELS = ["qwen2.5:3b", "llama3.2:3b", "llama3.1:8b"]
BACKFILL_N = 3000

CATEGORIES = ["To-Action", "Receipts", "Bookings", "Updates", "Promotions"]

SYSTEM = """You classify emails into exactly one category.

To-Action  - requires a decision, payment, reply, or click from the reader,
             especially anything with a deadline.
Receipts   - a transaction already completed; record-keeping only.
Bookings   - confirmation of something scheduled or reserved; reference only.
Updates    - low-priority informational content, no action ever needed.
Promotions - marketing content trying to sell something.

Precedence when an email fits two categories:
To-Action beats everything. Then Bookings over Receipts. Then Promotions
over Updates.

Give one short sentence of reasoning, then the category, then your
confidence from 0.0 to 1.0."""

SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "category": {"type": "string", "enum": CATEGORIES},
        "confidence": {"type": "number"},
    },
    "required": ["reasoning", "category", "confidence"],
}

# Realistic footer padding - real mail carries this bulk, so it belongs in a
# latency measurement rather than lorem ipsum.
FOOTER = (
    "\n\n---\nThis email and any attachments are confidential and intended "
    "solely for the addressee. If you have received it in error please notify "
    "the sender and delete it from your system. Any unauthorised copying, "
    "disclosure or distribution is strictly prohibited. We may monitor email "
    "traffic data for security and business purposes.\n\n"
    "You are receiving this message because you have an account with us or "
    "have opted in to communications. To change what you receive, update your "
    "preferences in your account settings. To stop receiving marketing email "
    "entirely, use the unsubscribe link below. Please do not reply to this "
    "address as the mailbox is not monitored.\n\n"
    "Registered office: 40 Example Street, London, EC1A 1AA. Registered in "
    "England and Wales, company number 01234567. VAT registration "
    "GB123456789. Authorised and regulated where applicable.\n"
    "Unsubscribe | Manage preferences | Privacy policy | Terms of service\n"
)

EMAILS = [
    {
        "expect": "To-Action",
        "sender": "billing@octopus.energy",
        "subject": "Your energy bill is ready - payment due 15 October",
        "body": "Hello, your statement for September is now available. The "
                "amount due is 87.42 and will be collected by Direct Debit on "
                "15 October. If your details have changed, please update them "
                "before the 12th to avoid a failed payment charge.",
    },
    {
        "expect": "Receipts",
        "sender": "auto-confirm@amazon.co.uk",
        "subject": "Your Amazon order #204-8837261 has shipped",
        "body": "Thanks for your order. Your parcel containing 1x USB-C cable "
                "(2m, braided) was dispatched today and is expected to arrive "
                "Tuesday. Total charged: 12.99 to Visa ending 4471. No action "
                "is needed.",
    },
    {
        "expect": "Bookings",
        "sender": "noreply@trainline.com",
        "subject": "Booking confirmed: London Euston to Manchester Piccadilly",
        "body": "Your tickets are confirmed for Thursday 14 November, "
                "departing 08:35, arriving 10:41. Coach C, seat 42A. Your "
                "booking reference is TRQ88H2. Please collect tickets from any "
                "self-service machine before travel.",
    },
    {
        "expect": "Updates",
        "sender": "news@linkedin.com",
        "subject": "5 new posts from people in your network",
        "body": "See what people you follow have been sharing this week, "
                "including a post about hiring trends in engineering and three "
                "new roles that match your profile. Catch up on the "
                "conversation.",
    },
    {
        "expect": "Promotions",
        "sender": "offers@uniqlo.co.uk",
        "subject": "48 hours only - 30% off all outerwear",
        "body": "Our biggest outerwear event of the season starts now. Take "
                "30% off jackets, coats and parkas in store and online until "
                "Sunday midnight. Shop early for the best selection of sizes.",
    },
]


def build_user_msg(email, body_chars):
    body = (email["body"] + FOOTER)[:body_chars]
    return (f"From: {email['sender']}\n"
            f"Subject: {email['subject']}\n\n"
            f"{body}")


def call(model, user_msg, timeout=600):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "format": SCHEMA,
        "stream": False,
        "options": {"temperature": 0},
    }
    req = urllib.request.Request(
        OLLAMA,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    wall = time.perf_counter() - t0
    return resp, wall


def check(raw):
    """Did the schema hold? Returns (ok, category, confidence, order_ok, err)."""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        return False, None, None, False, f"invalid JSON: {e}"
    keys = list(obj.keys())
    order_ok = keys[:3] == ["reasoning", "category", "confidence"]
    cat = obj.get("category")
    if cat not in CATEGORIES:
        return False, cat, obj.get("confidence"), order_ok, f"bad category {cat!r}"
    if not isinstance(obj.get("confidence"), (int, float)):
        return False, cat, None, order_ok, "confidence not a number"
    return True, cat, float(obj["confidence"]), order_ok, None


def run(model, body_chars, label):
    print(f"\n  {label} (body truncated to {body_chars} chars)")
    walls, correct, valid, order_ok_all = [], 0, 0, True
    prompt_toks, gen_toks, p_dur, g_dur = 0, 0, 0, 0

    for em in EMAILS:
        msg = build_user_msg(em, body_chars)
        try:
            resp, wall = call(model, msg)
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"    {em['expect']:<11} FAILED: {e}")
            continue

        raw = resp.get("message", {}).get("content", "")
        ok, cat, conf, order_ok, err = check(raw)
        walls.append(wall)
        prompt_toks += resp.get("prompt_eval_count", 0)
        gen_toks += resp.get("eval_count", 0)
        p_dur += resp.get("prompt_eval_duration", 0)
        g_dur += resp.get("eval_duration", 0)

        if ok:
            valid += 1
            if cat == em["expect"]:
                correct += 1
        order_ok_all &= order_ok

        mark = "ok " if cat == em["expect"] else "MISS"
        detail = err if err else f"{cat} @ {conf:.2f}"
        print(f"    {em['expect']:<11} {wall:6.1f}s  {mark}  {detail}")

    if not walls:
        return None

    med = statistics.median(walls)
    p_rate = prompt_toks / (p_dur / 1e9) if p_dur else 0
    g_rate = gen_toks / (g_dur / 1e9) if g_dur else 0
    hours = med * BACKFILL_N / 3600

    print(f"    -> median {med:.1f}s | prompt {p_rate:.0f} tok/s | "
          f"gen {g_rate:.0f} tok/s")
    print(f"    -> schema valid {valid}/{len(EMAILS)} | "
          f"key order {'held' if order_ok_all else 'BROKEN'} | "
          f"correct {correct}/{len(EMAILS)}")
    print(f"    -> {BACKFILL_N} emails = {hours:.1f} hours")
    return {"model": None, "median": med, "hours": hours,
            "correct": correct, "valid": valid, "order_ok": order_ok_all}


def main():
    print("Phase 0 spike - local model latency & schema compliance")
    print(f"Synthetic emails: {len(EMAILS)} | backfill estimate over "
          f"{BACKFILL_N} messages\n")

    results = []
    for model in MODELS:
        print(f"\n{'=' * 62}\n{model}\n{'=' * 62}")
        try:
            print("  warming up (loading model into RAM)...", flush=True)
            call(model, build_user_msg(EMAILS[0], 300))
        except Exception as e:
            print(f"  SKIPPED: {e}")
            continue
        for chars, label in ((300, "SHORT"), (2000, "LONG")):
            r = run(model, chars, label)
            if r:
                r["model"] = f"{model} [{label.lower()}]"
                results.append(r)

    print(f"\n\n{'=' * 62}\nSUMMARY\n{'=' * 62}")
    print(f"{'model [body]':<26} {'median':>8} {'backfill':>10} "
          f"{'schema':>8} {'right':>7}")
    for r in sorted(results, key=lambda x: x["median"]):
        print(f"{r['model']:<26} {r['median']:>7.1f}s {r['hours']:>9.1f}h "
              f"{r['valid']:>5}/{len(EMAILS)}  {r['correct']:>4}/{len(EMAILS)}")


if __name__ == "__main__":
    main()
