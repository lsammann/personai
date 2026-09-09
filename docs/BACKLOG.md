# Mailbox Survey — the existing backlog

**Findings only. Nothing here is a decision.**

Measured 2026-09-08 against the real mailbox by `scripts/spike_gmail.py`
(read-only, `gmail.readonly`). Re-run it to refresh; the mailbox grows by
roughly 18 messages a day, so the absolute numbers date quickly while the
shape probably doesn't.

"Backlog" here means the existing unprocessed mail a first backfill would
have to work through — 18,668 messages — as distinct from the ongoing trickle
the live poller handles.

---

## Headline

`DESIGN.md` was written assuming **~3,000** messages. The real figure is
**18,668** — a little over 6× the guess. Everything below follows from that.

The important qualifier: **this changes almost nothing about the app.** The
taxonomy, the classification method, the confidence mechanism, the thresholds,
the labelling rules, the poller and the webapp are all unaffected. What it
touches is one one-time operation (the backfill), one tuning parameter (body
truncation) and one sampling decision (the eval set). Those are noted at the
bottom.

---

## Volume

| | Messages |
|---|---|
| Mailbox total | 19,147 |
| Inbox | 17,013 |
| Sent / drafts / spam / trash | 439 / 40 / 44 / 3 |
| **Backfill scope** (`-in:sent -in:drafts -in:chats`) | **18,668** |
| — of which currently in the inbox | 16,976 |
| — already archived | 1,692 |

The inbox has essentially never been archived, which is the problem the
project exists to solve.

## Age — a recent explosion, not a long tail

```
2026   4,587  ###############        last 3 months    1,951   10%
2025   5,441  #################      last 6 months    3,541   19%
2024   4,960  ################       last 1 year      6,327   34%
2023   2,102  #######                last 2 years    11,719   63%
2022     902  ###                    last 3 years    16,024   86%
2021     446  #                      everything      18,668  100%
2020     228  #
2019       2
```

80% of the backlog arrived in the last three years, and everything before
2023 is only 1,578 messages. This matters because it limits how much a date
cutoff can buy: trimming to one year still leaves a third of the mail.

## Ongoing rate — not a problem

4,587 messages in ~250 days of 2026 is about **18 a day**. At the projected
~9.5s per message that is **under 3 minutes of inference per day**. Steady-
state operation is effectively free; the entire cost sits in the one-time
backfill.

## Body lengths — larger than the Phase 0 spikes assumed

```
p10      638      p50    7,553      p90    57,527
p25    1,981      p75   20,306      max   113,964
median 7,445
```

| Truncation | Bodies that fit whole |
|---|---|
| 500 chars | 8% |
| 1,000 chars | 12% |
| 2,000 chars | 28% |
| 4,000 chars | 32% |

The Phase 0 latency spikes ran at 300 and 1,200–2,000 characters — the 10th
to 28th percentile of real mail. **Those latency figures are therefore
optimistic.**

This matters more than it would elsewhere because the classification call
emits a single token, so cost is essentially `prompt_tokens ÷ prompt_eval
rate` — latency is close to linear in input length, with nothing else to
amortise it against.

Rough projection at a 1,500-char truncation (~375 tokens plus a ~300-token
system prompt), using Phase 0's measured prompt-eval rates:

| Scope | Messages | `llama3.1:8b` (~9.5s) | 3B (~3.8s) |
|---|---|---|---|
| last 3 months | 1,951 | 5.1h | 2.1h |
| last 6 months | 3,541 | 9.3h | 3.7h |
| last 1 year | 6,327 | 16.7h | 6.7h |
| last 2 years | 11,719 | 30.9h | 12.4h |
| everything | 18,668 | 49.3h | 19.7h |

Untruncated, at the median body length, the full backlog is on the order of
**160 hours**. Truncation is not a tuning preference here; without it the
backfill is impractical at any scope.

## Sender concentration — weaker than hoped

77 unique domains across a 150-message sample, **44 of them singletons**.

| | Share of sample |
|---|---|
| Top 5 domains | 22% |
| Top 10 | 36% |
| Top 20 | 53% |
| Top 30 | 67% |

A domain allowlist will skip perhaps a third of messages, not most of them.
Caveat: this sample is drawn from recent mail, and concentration across the
full ten years is probably higher, since repeat senders accumulate. Full
tally in `data/sender_domains.csv` (gitignored).

## Content skew

Twenty consecutive recent inbox subjects were **all** promotional or
notification mail — Depop, Macpac, Live Nation, Vinted, PUMA, Crust Pizza,
Collingwood FC, Grill'd, Spotify, LinkedIn, Flybuys, Qantas, Kathmandu,
Skyscanner. Zero `To Action`, zero `Personal`, zero `Receipts`, zero
`Bookings` in that window.

The six categories look adequate; the *distribution* across them is extremely
lopsided.

Two useful edge cases surfaced immediately:
- Vinted, *"Review and accept our new T&Cs"* — arguably `To Action`
- LinkedIn, *"Bridget Danaher — I want to connect"* — a real human, but an
  automated message, so `Updates` rather than `Personal`

---

## Options for the backfill — not chosen

Recorded so the reasoning exists when Phase 4 arrives. No commitment.

**A. Process everything.** 49h on the 8B. The backfill is resumable by
design, so this is roughly five overnight runs. Simple, complete, no new
concepts. Slow, and much of the work has little value.

**B. Date-scoped LLM pass.** Classify the last 6–12 months, leave older mail
alone. One to two nights. The argument: `To Action` is time-sensitive by
definition — a bill from 2022 is either paid or already a catastrophe — so
classification of old mail only buys retrieval-by-category, which for 2021
promotional mail is worth close to nothing. The cost is that old mail keeps
cluttering the inbox unless it is dealt with some other way.

**C. Layered: prefilter everywhere, LLM recent, bulk-archive the rest.**
Run the sender allowlist across all 18,668 first (string matching, no
inference, seconds) and label those properly for free. LLM-classify the
recent remainder. Archive whatever is left under a marker label — something
like `Agent/Bulk-Archived` — with no category, keeping it identifiable and
reversible. Gets the inbox usable in one night; adds a label and a concept to
the design, and leaves a chunk of mail without a category.

**Leaning:** C, then B, with A as the honest fallback if the extra machinery
turns out not to be worth it. Deliberately undecided.

**Unexamined:** whether a cheap pre-classification heuristic (List-Unsubscribe
header present, say) would outperform a domain allowlist. Most bulk mail
carries that header and almost no personal mail does, so it may be a stronger
free filter than the allowlist. Worth measuring before Phase 4.

---

## Where these findings should be consulted

Nothing here needs acting on now. It should be read at these points:

- **Phase 2 — eval sampling.** `PLAN.md` already says to sample deliberately
  rather than take the most recent 200 because they would be "80% promos".
  Confirmed, and probably understated. Random sampling would yield almost no
  `To Action` examples, and `To Action` recall is the headline metric.
- **Phase 2 — truncation tuning.** The body distribution above is the input
  to that decision. 1,500 chars is a placeholder, not a measurement.
- **Phase 4 — backfill.** `DESIGN.md`'s backfill section assumes ~3,000
  messages, a single undifferentiated pass, and no date scoping. Revisit it
  against this document before building.
- **Phase 4 — prefilter seeding.** `data/sender_domains.csv` is the derived
  allowlist seed, alongside the List-Unsubscribe idea above.

## Worth re-measuring later

- All of it, once the mailbox has moved on — it grows ~18/day.
- Sender concentration across the whole backlog rather than a recent sample.
- Body-length distribution on a larger sample; 40 messages is thin for
  percentiles as skewed as these.
