#!/usr/bin/env python3
"""Phase 0 spike: is the first-token distribution measuring content or letters?

Two questions, both of which affect what we write into DESIGN.md:

  1. LABEL BIAS - we ask the model to answer with a letter (A-F). If a model
     favours a slot regardless of what sits in it, the "confidence" is partly
     an artifact of the alphabet. Tested by running identical emails under
     three different letter->category mappings and checking whether the
     predicted CATEGORY stays stable.

  2. TEMPERATURE - if reported logprobs are post-temperature, then anything
     other than temperature 1.0 silently distorts the distribution, and the
     value must be pinned in production.

Also introduces the proposed sixth category, Personal, since the previous
spike showed human correspondence being archived at high confidence.

Stdlib only. Synthetic emails, not real mail.
"""

import csv
import json
import math
import os
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict

OLLAMA = "http://localhost:11434/api/chat"
MODELS = ["qwen2.5:3b", "llama3.2:3b", "llama3.1:8b"]
CSV_PATH = "data/spike_labels.csv"
BODY_CHARS = 1200

CATEGORIES = ["To-Action", "Receipts", "Bookings", "Updates", "Promotions",
              "Personal"]

DESCRIPTIONS = {
    "To-Action": "requires a decision, payment, reply, or click from the "
                 "reader, especially anything with a deadline",
    "Receipts": "a transaction already completed; record-keeping only",
    "Bookings": "confirmation of something scheduled or reserved; reference "
                "only",
    "Updates": "low-priority informational content, no action ever needed",
    "Promotions": "marketing content trying to sell something",
    "Personal": "written by a real person directly to the reader, not "
                "automated or bulk mail",
}

LETTERS = "ABCDEF"

# Three mappings. If a model tracks content rather than position, its
# predicted category is identical under all three.
PERMUTATIONS = {
    "P1-baseline": ["To-Action", "Receipts", "Bookings", "Updates",
                    "Promotions", "Personal"],
    "P2-reversed": ["Personal", "Promotions", "Updates", "Bookings",
                    "Receipts", "To-Action"],
    "P3-shuffled": ["Bookings", "Updates", "To-Action", "Personal",
                    "Receipts", "Promotions"],
}

PRECEDENCE = """Precedence when an email fits two categories:
To-Action beats everything except Personal. Then Bookings over Receipts.
Then Promotions over Updates. Anything written by a real human directly to
the reader is Personal."""


def system_prompt(order):
    lines = [f"{LETTERS[i]} = {cat} - {DESCRIPTIONS[cat]}"
             for i, cat in enumerate(order)]
    valid = ", ".join(LETTERS[:len(order)])
    return ("You classify emails into exactly one category.\n\n"
            + "\n".join(lines) + "\n\n" + PRECEDENCE
            + f"\n\nReply with exactly one character: {valid}. No "
              "explanation, no punctuation, no whitespace before it.")


FOOTER = (
    "\n\n---\nThis email and any attachments are confidential and intended "
    "solely for the addressee. You are receiving this message because you "
    "have an account with us. Registered office: 40 Example Street, London, "
    "EC1A 1AA. Company number 01234567.\n"
    "Unsubscribe | Manage preferences | Privacy policy\n"
)

EMAILS = [
    dict(id="bill", expect="To-Action", sender="billing@octopus.energy",
         subject="Your energy bill is ready - payment due 15 October",
         body="Your statement for September is available. The amount due is "
              "87.42 and will be collected by Direct Debit on 15 October. If "
              "your details have changed, update them before the 12th to "
              "avoid a failed payment charge.", pad=True),
    dict(id="amazon", expect="Receipts", sender="auto-confirm@amazon.co.uk",
         subject="Your Amazon order #204-8837261 has shipped",
         body="Thanks for your order. Your parcel containing 1x USB-C cable "
              "was dispatched today and arrives Tuesday. Total charged: "
              "12.99 to Visa ending 4471. No action is needed.", pad=True),
    dict(id="train", expect="Bookings", sender="noreply@trainline.com",
         subject="Booking confirmed: London Euston to Manchester Piccadilly",
         body="Your tickets are confirmed for Thursday 14 November, "
              "departing 08:35. Coach C, seat 42A. Booking reference "
              "TRQ88H2. Collect tickets from any machine before travel.",
         pad=True),
    dict(id="linkedin", expect="Updates", sender="news@linkedin.com",
         subject="5 new posts from people in your network",
         body="See what people you follow have been sharing this week, "
              "including a post about hiring trends in engineering.",
         pad=True),
    dict(id="uniqlo", expect="Promotions", sender="offers@uniqlo.co.uk",
         subject="48 hours only - 30% off all outerwear",
         body="Our biggest outerwear event of the season starts now. Take "
              "30% off jackets, coats and parkas until Sunday midnight.",
         pad=True),
    dict(id="flight", expect="Bookings", sender="confirmations@ba.com",
         subject="Booking confirmed - your receipt for flight BA1442",
         body="Flight BA1442, Edinburgh to London Heathrow, departs 06:55 on "
              "3 December. Booking reference JJ2K9P. Payment of 143.60 taken "
              "from Visa ending 4471. This email is your VAT receipt.",
         pad=True),
    dict(id="renewal", expect="To-Action", sender="billing@dropbox.com",
         subject="Your Dropbox Plus plan renews in 3 days",
         body="Your annual subscription renews automatically on 11 September "
              "for 95.88, charged to the card ending 4471. To change your "
              "plan or update payment details, visit account settings.",
         pad=True),
    dict(id="digest", expect="Updates", sender="jobs@linkedin.com",
         subject="Your job alert: 8 new Backend Engineer roles in London",
         body="We found 8 new roles you may be interested in, including "
              "Senior Backend Engineer at Monzo. Upgrade to Premium to see "
              "who else has applied.", pad=True),

    # --- the new category ---
    dict(id="pers_short", expect="Personal", sender="dan.willis@gmail.com",
         subject="Re: next week",
         body="yeah that works for me, see you then", pad=False),
    dict(id="pers_ask", expect="Personal", sender="rachel.okafor@gmail.com",
         subject="Saturday",
         body="Hey, something's come up on Saturday afternoon so I might be "
              "a bit late. Is 7 instead of 6 alright? Also do you still have "
              "my blue jumper from last time? No rush.", pad=False),
    dict(id="pers_news", expect="Personal", sender="mum@btinternet.com",
         subject="the garden",
         body="Dad finally finished the shed at the weekend, only took him "
              "four months. The apple tree has gone mad this year, we've got "
              "more than we know what to do with. Give us a ring when you "
              "get a minute, no hurry. Love you.", pad=False),
]


def build_user_msg(email):
    body = email["body"] + (FOOTER if email["pad"] else "")
    return (f"From: {email['sender']}\n"
            f"Subject: {email['subject']}\n\n{body}")[:BODY_CHARS + 200]


def post(payload, timeout=600):
    req = urllib.request.Request(
        OLLAMA, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r), time.perf_counter() - t0


def classify(model, email, order, temperature=1.0):
    n = len(order)
    letter_to_cat = {LETTERS[i]: order[i] for i in range(n)}
    resp, wall = post({
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt(order)},
            {"role": "user", "content": build_user_msg(email)},
        ],
        "stream": False, "logprobs": True, "top_logprobs": 20,
        "options": {"temperature": temperature, "num_predict": 1},
    })
    entries = resp.get("logprobs") or []
    if not entries:
        return None, None, {}, wall
    raw = {}
    for cand in entries[0].get("top_logprobs", []):
        tok = cand["token"].strip()
        if tok in letter_to_cat and tok not in raw:
            raw[tok] = cand["logprob"]
    if not raw:
        return None, None, {}, wall
    total = sum(math.exp(v) for v in raw.values())
    dist = {letter_to_cat[k]: math.exp(v) / total for k, v in raw.items()}
    for c in order:
        dist.setdefault(c, 0.0)
    pred = max(dist, key=dist.get)
    letter = LETTERS[order.index(pred)]
    return pred, letter, dist, wall


def main():
    print("Phase 0 spike - label-position bias & temperature")
    print(f"{len(EMAILS)} emails x {len(PERMUTATIONS)} letter mappings x "
          f"{len(MODELS)} models\n")

    rows = []
    for model in MODELS:
        print(f"\n{'=' * 78}\n{model}\n{'=' * 78}")
        try:
            post({"model": model,
                  "messages": [{"role": "user", "content": "hi"}],
                  "stream": False, "options": {"num_predict": 1}})
        except Exception as e:
            print(f"  SKIPPED: {e}")
            continue

        preds = defaultdict(dict)   # email -> perm -> (cat, letter, conf)
        for pname, order in PERMUTATIONS.items():
            for em in EMAILS:
                try:
                    cat, letter, dist, wall = classify(model, em, order)
                except (urllib.error.URLError, TimeoutError) as e:
                    print(f"  {em['id']} {pname} FAILED: {e}")
                    continue
                conf = dist.get(cat, 0.0) if cat else 0.0
                preds[em["id"]][pname] = (cat, letter, conf)
                rows.append({
                    "model": model, "permutation": pname, "email": em["id"],
                    "expected": em["expect"], "predicted": cat,
                    "letter_chosen": letter, "confidence": round(conf, 4),
                    "latency_s": round(wall, 2),
                    **{f"p_{c}": round(dist.get(c, 0.0), 4) for c in CATEGORIES},
                })

        print(f"\n  {'email':<11} {'expect':<11} " +
              "".join(f"{p.split('-')[0]:<22}" for p in PERMUTATIONS) +
              " stable?")
        stable_n = 0
        for em in EMAILS:
            got = preds[em["id"]]
            cats = [got.get(p, (None,))[0] for p in PERMUTATIONS]
            cells = ""
            for p in PERMUTATIONS:
                c, l, cf = got.get(p, (None, "-", 0.0))
                cells += f"{str(c)[:12]:<13}{l}@{cf:.2f}  "[:22]
            stable = len(set(cats)) == 1 and cats[0] is not None
            stable_n += stable
            print(f"  {em['id']:<11} {em['expect']:<11} {cells} "
                  f"{'yes' if stable else 'NO'}")

        letters = Counter(r["letter_chosen"] for r in rows
                          if r["model"] == model and r["letter_chosen"])
        total = sum(letters.values())
        spread = "  ".join(f"{l}:{letters.get(l, 0) / total:.0%}"
                           for l in LETTERS)
        print(f"\n  stable across all 3 mappings: {stable_n}/{len(EMAILS)}")
        print(f"  letter slots chosen:  {spread}   (even = ~17% each)")

    # ---- temperature probe ----
    print(f"\n\n{'=' * 78}\nTEMPERATURE: are reported logprobs post-scaling?"
          f"\n{'=' * 78}")
    probe = next(e for e in EMAILS if e["id"] == "amazon")
    order = PERMUTATIONS["P1-baseline"]
    for model in MODELS:
        line = f"  {model:<14}"
        try:
            for t in (0.5, 1.0, 2.0):
                _, _, dist, _ = classify(model, probe, order, temperature=t)
                top = max(dist.values()) if dist else float("nan")
                line += f"  t={t}: {top:.4f}"
        except Exception as e:
            line += f"  failed: {e}"
        print(line)
    print("\n  Identical values => temperature does not affect reported")
    print("  logprobs. Differing values => it does, and 1.0 must be pinned.")

    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nWritten to {CSV_PATH} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
