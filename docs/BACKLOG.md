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

> **Corrected 2026-09-09. The figures below conflate prose with HTML markup.**
> They were measured with an extractor whose fallback, for mail carrying no
> `text/plain` part, returns the raw HTML source. 23% of mail is HTML-only, so
> for roughly a quarter of the sample the "body length" recorded here is the
> length of `<td style="...">`, not of anything readable. See *Body structure*
> below for the corrected figures. The numbers in this block are retained
> because the latency argument that follows them still holds — the model was
> being sent that markup, and paying for it.

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

## Body structure — what the model actually receives

Measured 2026-09-10 by `scripts/measure_bodies.py` over 250 messages sampled
(seed 11) from the Phase 2 eval frame, pinned to absolute bounds:
`after:1757462400 before:1788998400 -in:sent -in:drafts -in:chats` (6,314
messages). Read-only; nothing written to disk. Figures are unaffected by the
`message/rfc822` handling described below, which no message in the sample
exercised.

> **Use absolute dates when sampling.** `newer_than:1y` is relative, so it
> names a different population every day and a seeded sample drawn through it
> is not reproducible. Measured directly: the same seed over the same nominal
> frame returned a largely different 150 messages on consecutive days.

| Cohort | Share |
|---|---|
| Has a real `text/plain` part | 75.6% |
| **HTML-only** | **24.4%** |
| No text part at all | 0% |
| — of which stub `text/plain` parts | 0.8% |

At n=250 the HTML-only share carries roughly ±5 points. Three samples across
two days gave 23.3%, 16.0% and 24.4% — call it **about one message in four**,
and do not read the third decimal.

A *stub* is a `text/plain` part under 200 characters sitting beside more than
twice that in stripped HTML — "View this email in your browser" — where the
real content exists only as HTML. Rare, but it fails silently: the extractor
succeeds and returns a body indistinguishable from a genuinely short email.

**Lengths, separated by cohort.** This is the corrected input to the
truncation decision:

| | p10 | p50 | p90 |
|---|---|---|---|
| Old extractor output (the figures above) | 1,203 | **7,555** | 79,216 |
| `text/plain` parts — actual prose | 1,052 | **5,637** | 37,484 |
| `text/html` raw source | 15,153 | 59,313 | 122,867 |
| `text/html` tag-stripped — actual prose | 640 | **1,598** | 3,818 |
| **Selected body — what the model now reads** | 705 | **3,874** | 23,332 |

Stripping keeps **2.9%** of the HTML source. **26.0%** of selected bodies fit
whole inside the current 1,500-character budget.

**Consequences.**

- For a quarter of all mail, the budget was being spent almost entirely on
  markup. The first 300 characters of a real message under the old extractor:

  ```
  <html dir="ltr" lang="en">\n  <head>\n    <meta http-equiv="Content-Type" ...
  ```

  The same message now opens `"Luke Sammann, this code expires in 10 minutes.
  ... Your security code is ..."` — a PayPal 2FA mail, unmistakable once
  readable. The old behaviour was both less accurate *and* no faster, since
  the markup still had to be tokenised.
- **One `body_chars` is doing two different jobs.** At 1,500 it captures
  nearly a whole stripped HTML message (p50 1,598) but only the first quarter
  of a typical plain-text one (p50 5,637). The Phase 2 sweep will show this;
  before the correction it was hidden inside a blended average.
- Attachments are never fetched — Gmail returns an `attachmentId` rather than
  data, and the walk skips those parts. A bill existing only as an attached
  PDF is invisible to the classifier. See `DESIGN.md` → Classification logic.

## Forwarded mail and reply chains

- **`message/rfc822` (forward "as attachment"): 0 of 250, 0.0%** — 95% upper
  bound about 1.2%, so up to ~75 messages across the frame.
- **Gmail's default Forward is *inline*,** not as-attachment: the original
  lands in the same text part, below the note. That is 100% of the forwards
  present in this mailbox.
- **So an attached message is appended after the covering note**, with a
  forwarded-message divider, reproducing what inline forwarding already
  produces. Reading only the note would make one user action classify
  differently depending on which forward button was pressed. Order is the
  substance of the rule: truncation cuts from the start, so the human's words
  always survive and the forwarded content fills the remaining budget — a
  friend forwarding a promotion stays `Personal`, while a friend forwarding an
  invoice still has "invoice" and "due" within reach.
- Whether the model actually reads a forwarded note as `Personal` is a prompt
  question the eval set answers.
- **Quoted reply history: 2 of 250, 0.8%** of the frame, averaging 71% of the
  body, with the new text fitting inside 1,500 characters in both cases.
  Far too thin to conclude anything — the random frame is almost entirely bulk
  mail. The `S_human` stratum over-samples reply chains by design and is where
  this gets measured.

## Gmail API quota — a constraint on bulk fetching

Two sweeps died on `403 rateLimitExceeded` between the **150th and 175th**
`messages.get`, and seven backoff attempts spanning a minute did not reopen
the window — so it is not a per-minute burst limit, and reacting to it does
not work. Pacing does: 250 fetches at a 0.5s delay completed with zero
retries.

`gmail_client.throttle()` is opt-in rather than built into `fetch()`, because
the labelling CLI fetches one message per human decision, roughly every thirty
seconds, and would gain nothing from sleeping. **Anything that prefetches in
bulk — notably building the eval content cache — must pace.**

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
