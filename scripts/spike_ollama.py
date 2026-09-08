#!/usr/bin/env python3
"""Phase 0 spike: can we get a *meaningful* confidence out of a local model?

Compares two ways of producing confidence for the same classification:

  selfreport - the model writes reasoning, then names a category, then states
               a confidence number. This is what DESIGN.md currently specifies.
  logprob    - the model emits a single letter (A-E) as its first and only
               token. We read the token distribution, keep the five category
               letters, and renormalise them into a probability distribution.
               No chain-of-thought, so nothing conditions the answer.

The question is not "which is more accurate" - it is "which confidence
actually tracks correctness". The headline number is mean confidence when
right vs when wrong. If those are equal, the signal is dead.

Writes a CSV so the raw distributions can be inspected by hand.
Stdlib only - runs without a venv. Synthetic emails, not real mail.
"""

import csv
import json
import math
import os
import statistics
import time
import urllib.error
import urllib.request

OLLAMA = "http://localhost:11434/api/chat"
MODELS = ["qwen2.5:3b", "llama3.2:3b", "llama3.1:8b"]
CSV_PATH = "data/spike_confidence.csv"
BODY_CHARS = 1200

# Single-token labels. Category names would tokenize into several tokens and
# first tokens can collide ("Receipts"/"Reminders" both start "Re").
LETTERS = {
    "A": "To-Action",
    "B": "Receipts",
    "C": "Bookings",
    "D": "Updates",
    "E": "Promotions",
}
CATEGORIES = list(LETTERS.values())
LETTER_OF = {v: k for k, v in LETTERS.items()}

DEFINITIONS = """A = To-Action  - requires a decision, payment, reply, or click from the
                reader, especially anything with a deadline.
B = Receipts   - a transaction already completed; record-keeping only.
C = Bookings   - confirmation of something scheduled or reserved; reference
                 only.
D = Updates    - low-priority informational content, no action ever needed.
E = Promotions - marketing content trying to sell something.

Precedence when an email fits two categories:
To-Action beats everything. Then Bookings over Receipts. Then Promotions
over Updates."""

SYSTEM_LOGPROB = f"""You classify emails into exactly one category.

{DEFINITIONS}

Reply with exactly one character: A, B, C, D or E. No explanation, no
punctuation, no whitespace before it."""

SYSTEM_SELFREPORT = f"""You classify emails into exactly one category.

{DEFINITIONS}

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

FOOTER = (
    "\n\n---\nThis email and any attachments are confidential and intended "
    "solely for the addressee. If you have received it in error please notify "
    "the sender and delete it from your system. Any unauthorised copying, "
    "disclosure or distribution is strictly prohibited.\n\n"
    "You are receiving this message because you have an account with us or "
    "have opted in to communications. To change what you receive, update your "
    "preferences in your account settings. Please do not reply to this "
    "address as the mailbox is not monitored.\n\n"
    "Registered office: 40 Example Street, London, EC1A 1AA. Registered in "
    "England and Wales, company number 01234567.\n"
    "Unsubscribe | Manage preferences | Privacy policy | Terms of service\n"
)

# `expect` follows DESIGN.md's precedence rule. `ambiguous` marks cases where
# a human could reasonably disagree - these SHOULD score lower confidence, and
# are the whole point of the test.
EMAILS = [
    dict(id="bill", expect="To-Action", ambiguous=False,
         sender="billing@octopus.energy",
         subject="Your energy bill is ready - payment due 15 October",
         body="Hello, your statement for September is now available. The "
              "amount due is 87.42 and will be collected by Direct Debit on "
              "15 October. If your details have changed, please update them "
              "before the 12th to avoid a failed payment charge."),
    dict(id="amazon", expect="Receipts", ambiguous=False,
         sender="auto-confirm@amazon.co.uk",
         subject="Your Amazon order #204-8837261 has shipped",
         body="Thanks for your order. Your parcel containing 1x USB-C cable "
              "(2m, braided) was dispatched today and is expected to arrive "
              "Tuesday. Total charged: 12.99 to Visa ending 4471. No action "
              "is needed."),
    dict(id="train", expect="Bookings", ambiguous=False,
         sender="noreply@trainline.com",
         subject="Booking confirmed: London Euston to Manchester Piccadilly",
         body="Your tickets are confirmed for Thursday 14 November, "
              "departing 08:35, arriving 10:41. Coach C, seat 42A. Your "
              "booking reference is TRQ88H2. Please collect tickets from any "
              "self-service machine before travel."),
    dict(id="linkedin", expect="Updates", ambiguous=False,
         sender="news@linkedin.com",
         subject="5 new posts from people in your network",
         body="See what people you follow have been sharing this week, "
              "including a post about hiring trends in engineering. Catch up "
              "on the conversation."),
    dict(id="uniqlo", expect="Promotions", ambiguous=False,
         sender="offers@uniqlo.co.uk",
         subject="48 hours only - 30% off all outerwear",
         body="Our biggest outerwear event of the season starts now. Take "
              "30% off jackets, coats and parkas in store and online until "
              "Sunday midnight. Shop early for the best selection of sizes."),

    # --- deliberately ambiguous: low confidence is the CORRECT answer ---
    dict(id="flight", expect="Bookings", ambiguous=True,
         sender="confirmations@ba.com",
         subject="Booking confirmed - your receipt for flight BA1442",
         body="Thank you for your booking. Flight BA1442, Edinburgh to "
              "London Heathrow, departs 06:55 on 3 December. Booking "
              "reference JJ2K9P. Payment of 143.60 was taken from Visa "
              "ending 4471. This email is your VAT receipt; no further "
              "action is required."),
    dict(id="renewal", expect="To-Action", ambiguous=True,
         sender="billing@dropbox.com",
         subject="Your Dropbox Plus plan renews in 3 days",
         body="Your annual Dropbox Plus subscription will renew automatically "
              "on 11 September for 95.88, charged to the card ending 4471. "
              "If you would like to change your plan or update your payment "
              "details, you can do so in your account settings."),
    dict(id="digest", expect="Updates", ambiguous=True,
         sender="jobs-listings@linkedin.com",
         subject="Your job alert: 8 new Backend Engineer roles in London",
         body="Based on your profile we found 8 new roles you may be "
              "interested in, including Senior Backend Engineer at Monzo and "
              "Platform Engineer at Cleo. Upgrade to LinkedIn Premium to see "
              "who else has applied and how you compare to other candidates."),
    dict(id="personal", expect=None, ambiguous=True,
         sender="dan.willis@gmail.com",
         subject="Re: next week",
         body="yeah that works for me, see you then"),
]


def build_user_msg(email):
    body = (email["body"] + FOOTER)[:BODY_CHARS]
    return (f"From: {email['sender']}\n"
            f"Subject: {email['subject']}\n\n{body}")


def post(payload, timeout=600):
    req = urllib.request.Request(
        OLLAMA, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    return resp, time.perf_counter() - t0


def classify_logprob(model, email):
    """Single letter, first token only. Renormalise over the five valid letters.

    Temperature 1.0 so the reported distribution isn't distorted by temperature
    scaling; argmax is taken here rather than by the sampler.
    """
    resp, wall = post({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_LOGPROB},
            {"role": "user", "content": build_user_msg(email)},
        ],
        "stream": False,
        "logprobs": True,
        "top_logprobs": 20,
        "options": {"temperature": 1.0, "num_predict": 1},
    })

    entries = resp.get("logprobs") or []
    if not entries:
        return None, {}, None, wall, "no logprobs returned"

    raw = {}
    for cand in entries[0].get("top_logprobs", []):
        tok = cand["token"].strip()          # guard leading-space variants
        if tok in LETTERS and tok not in raw:
            raw[tok] = cand["logprob"]

    if not raw:
        got = resp.get("message", {}).get("content", "")
        return None, {}, None, wall, f"no category letter in top-20 (said {got!r})"

    total = sum(math.exp(lp) for lp in raw.values())
    dist = {LETTERS[k]: math.exp(v) / total for k, v in raw.items()}
    for c in CATEGORIES:
        dist.setdefault(c, 0.0)

    pred = max(dist, key=dist.get)
    missing = 5 - len(raw)
    note = f"{missing} letter(s) outside top-20" if missing else ""
    return pred, dist, dist[pred], wall, note


def classify_selfreport(model, email):
    """DESIGN.md's current design: reasoning, then category, then confidence."""
    resp, wall = post({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_SELFREPORT},
            {"role": "user", "content": build_user_msg(email)},
        ],
        "format": SCHEMA,
        "stream": False,
        "options": {"temperature": 0},
    })
    raw = resp.get("message", {}).get("content", "")
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, {}, None, wall, f"invalid JSON: {e}"
    cat = obj.get("category")
    if cat not in CATEGORIES:
        return None, {}, None, wall, f"bad category {cat!r}"
    return cat, {}, float(obj.get("confidence", 0.0)), wall, ""


def main():
    print("Phase 0 spike - is the confidence signal meaningful?")
    print(f"{len(EMAILS)} synthetic emails "
          f"({sum(e['ambiguous'] for e in EMAILS)} deliberately ambiguous)")
    print(f"body truncated to {BODY_CHARS} chars\n")

    rows = []
    for model in MODELS:
        print(f"\n{'=' * 74}\n{model}\n{'=' * 74}")
        try:
            post({"model": model,
                  "messages": [{"role": "user", "content": "hi"}],
                  "stream": False, "options": {"num_predict": 1}})
        except Exception as e:
            print(f"  SKIPPED: {e}")
            continue

        for method, fn in (("logprob", classify_logprob),
                           ("selfreport", classify_selfreport)):
            print(f"\n  {method}")
            print(f"    {'email':<10} {'expect':<11} {'got':<11} "
                  f"{'conf':>6}  {'':<4} note")
            for em in EMAILS:
                try:
                    pred, dist, conf, wall, note = fn(model, em)
                except (urllib.error.URLError, TimeoutError) as e:
                    print(f"    {em['id']:<10} FAILED: {e}")
                    continue

                # `personal` fits no category, so correctness is undefined -
                # it is here purely to see whether confidence drops.
                if em["expect"] is None:
                    correct = None
                    mark = "n/a"
                else:
                    correct = (pred == em["expect"])
                    mark = "ok" if correct else "MISS"

                amb = "amb" if em["ambiguous"] else ""
                cstr = f"{conf:.3f}" if conf is not None else "  -  "
                print(f"    {em['id']:<10} {str(em['expect']):<11} "
                      f"{str(pred):<11} {cstr:>6}  {mark:<4} {amb} {note}")

                rows.append({
                    "model": model, "method": method, "email": em["id"],
                    "ambiguous": em["ambiguous"], "expected": em["expect"],
                    "predicted": pred,
                    "correct": "" if correct is None else correct,
                    "confidence": "" if conf is None else round(conf, 4),
                    "latency_s": round(wall, 2),
                    **{f"p_{c}": round(dist.get(c, 0.0), 4) if dist else ""
                       for c in CATEGORIES},
                    "note": note,
                })

    # ---- the headline: does confidence separate right from wrong? ----
    print(f"\n\n{'=' * 74}\nDOES CONFIDENCE TRACK CORRECTNESS?\n{'=' * 74}")
    print(f"{'model':<14} {'method':<11} {'acc':>6} {'conf|right':>11} "
          f"{'conf|wrong':>11} {'gap':>7} {'conf|amb':>9}")
    for model in MODELS:
        for method in ("logprob", "selfreport"):
            sub = [r for r in rows if r["model"] == model
                   and r["method"] == method and r["confidence"] != ""]
            scored = [r for r in sub if r["correct"] != ""]
            if not scored:
                continue
            right = [r["confidence"] for r in scored if r["correct"]]
            wrong = [r["confidence"] for r in scored if not r["correct"]]
            amb = [r["confidence"] for r in sub if r["ambiguous"]]
            mr = statistics.mean(right) if right else float("nan")
            mw = statistics.mean(wrong) if wrong else float("nan")
            gap = mr - mw if right and wrong else float("nan")
            print(f"{model:<14} {method:<11} {len(right)}/{len(scored):<4} "
                  f"{mr:>11.3f} {mw:>11.3f} {gap:>7.3f} "
                  f"{statistics.mean(amb):>9.3f}")
    print("\ngap = mean confidence when right minus when wrong.")
    print("A gap near zero means the confidence carries no information.")
    print("conf|amb should sit BELOW conf|right if ambiguity is detected.")

    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nFull per-email distributions written to {CSV_PATH} "
          f"({len(rows)} rows)")


if __name__ == "__main__":
    main()
