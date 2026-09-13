#!/usr/bin/env python3
"""Live smoke test of the classification core against a real Ollama model.

Not a unit test and not the eval set. Six hand-written emails, no scoring.
The questions it answers are about plumbing, not accuracy:

  - does the prompt actually produce a single category letter?
  - is `retained_mass` high, i.e. does the model comply with the output
    format at all? A low value means confidence is measured over a sliver of
    the distribution and is worth less than it looks.
  - does the distribution look readable, and does the runner-up make sense?
  - does an HTML-only email reach the model as readable words rather than
    as markup? That is 23% of real mail; see docs/BACKLOG.md.

Worth re-running whenever the model, the prompt or `prompt_id` changes -
it is the cheapest way to catch a prompt that a new model simply ignores.
Accuracy is measured by `eval/run_eval.py` against real hand-labelled mail;
these six synthetic emails prove nothing about it.

Writes nothing: no labels, no log rows, no Gmail call of any kind.

    uv run python scripts/smoke_classify.py
    uv run python scripts/smoke_classify.py --model qwen2.5:3b
"""

from __future__ import annotations

import argparse
import time

from app import classifier, config, decision, message_body

# (label, sender, subject, text_plain, text_html). The fifth is deliberately
# ambiguous: a flight confirmation that is also a VAT receipt, which the
# prompt's precedence rule resolves as Bookings over Receipts. The sixth
# carries no plain part at all - 23% of real mail is HTML-only, and before
# `message_body` those messages reached the model as raw markup.
EMAILS = [
    (
        "bill / expect To Action",
        "billing@octopus.energy",
        "Your energy bill is ready - payment due 15 October",
        (
            "Hello, your statement for September is now available. The amount due "
            "is 87.42 and will be collected by Direct Debit on 15 October. If your "
            "details have changed, please update them before the 12th to avoid a "
            "failed payment charge."
        ),
        "",
    ),
    (
        "receipt / expect Receipts",
        "auto-confirm@amazon.co.uk",
        "Your Amazon order #204-8837261 has shipped",
        (
            "Thanks for your order. Your parcel containing 1x USB-C cable was "
            "dispatched today and is expected to arrive Tuesday. Total charged: "
            "12.99 to Visa ending 4471. No action is needed."
        ),
        "",
    ),
    (
        "human note / expect Personal",
        "dan.willis@gmail.com",
        "Re: next week",
        "yeah that works for me, see you then",
        "",
    ),
    (
        "promo / expect Promotions",
        "offers@depop.com",
        "48 hours only - 30% off everything",
        (
            "Our biggest event of the season starts now. Take 30% off in app and "
            "online until Sunday midnight. Shop early for the best selection."
        ),
        "",
    ),
    (
        "ambiguous / Bookings over Receipts",
        "confirmations@ba.com",
        "Booking confirmed - your receipt for flight BA1442",
        (
            "Thank you for your booking. Flight BA1442, Edinburgh to London "
            "Heathrow, departs 06:55 on 3 December. Booking reference JJ2K9P. "
            "Payment of 143.60 was taken from Visa ending 4471. This email is your "
            "VAT receipt; no further action is required."
        ),
        "",
    ),
    (
        "html-only / expect Promotions",
        "shop@vailresorts.com.au",
        "Bring a Mate Invite",
        "",
        (
            '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">'
            "<html><head><style>.btn{font-size:14px;color:#fff}</style></head>"
            "<body><!-- [if mso]><table><tr><td><![endif]-->"
            "<p>&nbsp;</p><div>You must have some great mates!</div>"
            "<div>You have been invited to access a discounted lift ticket. "
            "Save up to 50% off a one day lift ticket during the 2026 season. "
            "Click below to purchase online.</div>"
            "<script>track('open')</script></body></html>"
        ),
    ),
]


def main() -> None:
    cfg = config.Config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=cfg.ollama_model)
    parser.add_argument("--body-chars", type=int, default=cfg.body_chars)
    parser.add_argument("--prompt", default=classifier.DEFAULT_PROMPT_ID)
    args = parser.parse_args()

    print(
        f"model={args.model}  body_chars={args.body_chars}  "
        f"prompt={args.prompt} ({classifier.prompt_hash(args.prompt)})  "
        f"T={cfg.confidence_threshold}  F={cfg.to_action_floor}\n"
    )

    for name, sender, subject, plain, html in EMAILS:
        started = time.perf_counter()
        try:
            result = classifier.classify(
                sender, subject, plain, html,
                model=args.model, body_chars=args.body_chars,
                prompt_id=args.prompt,
            )
        except classifier.ClassifierError as exc:
            print(f"{name}\n   FAILED: {type(exc).__name__}: {exc}\n")
            continue
        wall = time.perf_counter() - started

        chosen = decision.decide(
            result.distribution,
            confidence_threshold=cfg.confidence_threshold,
            to_action_floor=cfg.to_action_floor,
        )
        # The same pure function classify() used internally, called again only
        # to report which branch fired. Deterministic, so there is no second
        # rule here that could drift from the one the model actually saw.
        selection = message_body.select_body(plain, html)
        top = sorted(result.distribution.items(), key=lambda kv: -kv[1])[:3]
        spread = "  ".join(f"{c.value}={p:.3f}" for c, p in top if p > 0.001)

        print(name)
        print(
            f"   -> {result.category.value:11} conf={result.confidence:.3f}  "
            f"retained={result.retained_mass:.3f}  {wall:.1f}s"
        )
        print(f"      {spread}")
        print(
            f"      body={selection.source} ({len(selection.text)} chars"
            f"{'' if selection.parsed_ok else ', PARSE FAILED'})"
        )
        print(
            f"      labels={list(chosen.labels_add)} "
            f"inbox={'REMOVED' if chosen.labels_remove else 'kept'}"
            f"{'  [To Action floor fired]' if chosen.to_action_override else ''}"
        )
        if result.missing_letters:
            print(f"      letters outside top-20: {result.missing_letters}")
        print()

    print(
        "The first call includes model load; later ones are warm. These bodies "
        "are short —\nreal mail truncated to "
        f"{args.body_chars} chars is slower. See docs/BACKLOG.md."
    )


if __name__ == "__main__":
    main()
