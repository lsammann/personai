# Email Agent — Design Doc (v1)

## Overview

A personal system that watches my Gmail inbox and automatically sorts mail
into a small set of labeled buckets, using a locally-run LLM (Ollama) for
classification. Controlled through a small local webapp (start/stop, config,
dry-run, one-off backfill, metrics). Built as a portfolio project — the goal
is to understand every architectural decision well enough to defend it in an
interview, not just to have it working.

**Framing note — this is a pipeline, not an agent.** The control flow is
fixed at design time: fetch → pre-filter → one LLM call → map result to
labels. The model returns a value; my code decides what happens next. That is
the correct choice for a single-decision task with a fixed action space —
a tool-calling loop would add latency, non-determinism, and evaluation
difficulty while buying no accuracy. Two genuinely agentic designs for this
problem are specified in Future Enhancements, along with the conditions under
which they'd start to pay for themselves. Knowing why the pipeline is right
here is the point; claiming the system is "agentic" when it isn't would
invite a question I couldn't win.

**Secondary goals for interview value:**
- Demonstrate a triggered, autonomous system with real write access and real
  blast radius — and the operational discipline that requires (idempotency,
  dry-run, failure semantics, an undo path)
- Demonstrate a measured classifier: a hand-labeled eval set, an accuracy
  number, and a calibration check — not "it feels about right"
- Demonstrate deliberate scoping decisions (why polling before push, why a
  log instead of a DB, why no fallback model yet, why per-message over
  per-thread)
- Leave a clear, natural extension path toward RAG (retrieval-augmented
  few-shot classification), event-driven infra (Pub/Sub), and a genuinely
  agentic triage loop as v2 work

**Terminology note:** in Gmail, "archiving" *is* removing the `INBOX` label.
Nothing is moved or deleted — the message simply stops appearing in the inbox
view and remains accessible via its `Agent/` label, All Mail, and search.
This doc uses **"remove `INBOX`"** throughout for precision.

---

## V1 Requirements

### Category taxonomy

Eight Gmail labels, all nested under an `Agent/` parent for visibility and
easy querying:

| Label | Definition | Keeps `INBOX`? | Model can output? |
|---|---|---|---|
| `Agent/To-Action` | Requires a decision, payment, reply, or click from me — especially anything with a deadline | Yes | Yes |
| `Agent/Receipts` | A transaction already completed; record-keeping only, no action needed | No | Yes |
| `Agent/Bookings` | Confirmation of something scheduled/reserved (appointment, flight, reservation); reference only | No | Yes |
| `Agent/Updates` | Low-priority informational content, no action ever needed (newsletters, LinkedIn digests) | No | Yes |
| `Agent/Promotions` | Marketing/sales content trying to get me to buy something | No | Yes |
| `Agent/Personal` | Written by a real person directly to me, not automated or bulk mail | Yes | Yes |
| `Agent/Needs-Review` | Classifier confidence below threshold; holding label until I manually re-file it | Yes | No — applied by the app |
| `Agent/Processed` | Marker applied alongside every category label; the sole dedup mechanism | N/A (never affects inbox state) | No — applied by the app |

`Agent/Needs-Review` is not a "real" category — it should be empty most of
the time. Every manual re-file out of it is a labeled example, captured by
the classification log (below) for future retrieval-augmented
classification.

**Label naming decision: hyphens, not spaces.** Gmail search syntax doesn't
accept unquoted spaces, so space-separated label names require
`label:"Agent/To Action"` everywhere, and a single missing pair of quotes is
a silent bug that returns wrong results rather than an error. Hyphenated
names query cleanly as `label:Agent/To-Action`. Decided once here; the
display cost is trivial.

**The action space is nearly binary, and that matters.** Four of the six
real categories (`Receipts`, `Bookings`, `Updates`, `Promotions`) produce
identical behaviour: label it, remove `INBOX`. `To-Action` and `Personal`
both keep it. So a Receipts↔Bookings confusion costs nothing, and so does a
To-Action↔Personal one — while a `To-Action` false negative means a missed
bill. The categories exist for retrieval and tidiness; the only decision with
consequences is *does this need to stay visible*. This asymmetry drives the
evaluation strategy and the asymmetric threshold rule below.

**Why `Personal` exists.** The other five categories all describe
machine-generated mail — receipts, confirmations, newsletters, marketing,
bills. Human correspondence fits none of them, and forcing a choice among
five wrong options produces an arbitrary answer with arbitrary confidence.
The original plan was to let the confidence floor catch personal mail and
route it to `Needs-Review`, but Phase 0 measured that failing: on a two-line
note from a friend, `qwen2.5:3b` returned `Updates` at **0.953** confidence.
It would have been silently archived. `Personal` keeps `INBOX`, so a
misfiling between it and `To-Action` is operationally harmless — no attempt
is made to split personal mail into actionable and not.

### Scope of processing

- Process **all** inbox mail, not just Gmail's `category:primary` tab.
  Gmail's own Social/Promotions/Updates tabs are currently ignored by me
  entirely, so real actionable mail can hide there unseen — the agent should
  catch it.
- Query for unprocessed mail:
  `in:inbox -label:Agent/Processed -in:sent -in:drafts -in:chats`
- The sent/drafts/chats exclusions matter more for backfill than for the live
  poller, but they're applied in both for consistency — see Backfill.
- **Per-message, not per-thread.** Each message is classified independently.
  Gmail displays threads but stores messages, so a long conversation can end
  up with more than one `Agent/` label across its messages, and the thread
  will appear under both. Accepted deliberately for v1: the overwhelming
  majority of this inbox is single-message mail (receipts, promos,
  newsletters, confirmations) where threading never comes up. Thread-aware
  handling is a future enhancement.

### Labeling actions

For every successfully classified message, the agent applies, **in a single
`messages.modify` call**:
- `addLabelIds`: the chosen category label (or `Agent/Needs-Review`), plus
  `Agent/Processed`
- `removeLabelIds`: `INBOX` — except for `Agent/To-Action` and
  `Agent/Needs-Review`, which retain it

**One atomic call, not three sequential ones.** Gmail's `messages.modify`
accepts adds and removes together. Splitting this into separate calls means a
crash or API error between them can leave a message categorized but not
`Agent/Processed` — which makes it eligible for reclassification on the next
run and can leave it carrying two different category labels.

Removing `INBOX` is the agent's most consequential write action and the
mechanism behind the end-state goal: **the inbox only ever contains
`Agent/To-Action` and `Agent/Needs-Review` items.**

### Dry-run mode

A config flag (`dry_run: true`) that runs the full pipeline — fetch,
pre-filter, classify, decide labels, **write to the classification log** —
but applies no label changes and removes no `INBOX` labels. Intended actions
are reported to the webapp and to stdout.

This is not optional polish: the first backfill run touches ~3000 emails
using a model whose accuracy hasn't been validated yet. Dry-run first,
inspect the log, then enable writes.

Because dry runs write to the log, the Metrics view works normally during a
dry run. This is the whole point — a dry run whose output vanishes to stdout
is much less useful than one you can query afterwards.

**Undo path:** because every agent-touched message carries `Agent/Processed`,
a bad run is recoverable — a script can query `label:Agent/Processed`, strip
all `Agent/` labels, and restore `INBOX`. Worth writing this script *before*
the first live run, not after.

**`undo_run.py` must be time-scoped.** An unscoped undo run, executed after
several good weeks, would dump thousands of correctly-archived emails back
into the inbox. Gmail search accepts epoch seconds (`after:1725750000`), so
the time window is a **required argument with no default**. Bounding by run
id isn't possible without a DB; bounding by time is, and is sufficient.

### Pre-filter (before calling the model)

- A sender-domain allowlist (JSON list of known promotional domains) checked
  first. A match routes straight to `Agent/Promotions` + `Agent/Processed`,
  `INBOX` removed, no LLM call made.
- Anything not matched goes to the classifier with the five real categories
  as options.
- The allowlist starts short and manually seeded, growing as I notice repeat
  senders that keep landing in Needs-Review or get misclassified.
- Pre-filter hits are written to the classification log the same as model
  results, tagged with their source, so metrics account for them.

### Classification logic

The design below is what Phase 0 measured rather than what it assumed — the
spikes and their findings are recorded in `docs/PLAN.md`.

- **One forward pass, one token.** The six category definitions are mapped to
  letters `A`–`F` in the system prompt and the model is asked to reply with
  exactly one character. Called with `num_predict: 1`, `logprobs: true`,
  `top_logprobs: 20`.
- **Confidence is the renormalised token distribution, not a self-reported
  number.** Take the returned top-20 token distribution, keep the entries
  that are valid category letters, exponentiate the logprobs and normalise so
  they sum to 1. That yields a probability distribution over the six
  categories; the prediction is the argmax and the confidence is its
  probability. Any letter falling outside the top 20 gets probability 0 and
  the classification log records that it happened.
- **Single-token letter labels, not category names.** Category words tokenize
  into several tokens and their first tokens collide (`Receipts` and
  `Reminders` both begin `Re`), which makes the first-token distribution
  unreadable. Letters are reliably one token each.
- **No chain-of-thought.** Reasoning is not requested. Emitting reasoning
  first conditions the category on that reasoning, so the token distribution
  would measure agreement-with-its-own-argument rather than confidence in the
  answer. Phase 0 measured the accuracy cost of dropping it at roughly zero
  (identical scores on two of three models, one point worse on the third).
  The full six-way distribution goes into the classification log, which is
  more useful for debugging than prose would have been.
- **Category precedence rule, stated explicitly in the prompt.** Several
  emails legitimately belong to two categories — a flight receipt is both a
  Receipt and a Booking; a sale email from a shop I use is both Promotions
  and Updates. Without a tiebreak the model dithers and floods Needs-Review
  with items where *either answer was fine*. The prompt states a fixed
  precedence: anything written by a real human is `Personal`; otherwise
  `To-Action` beats everything; then `Bookings` over `Receipts`; then
  `Promotions` over `Updates`.
- **Confidence threshold starts at 0.8.** Below this → `Agent/Needs-Review`
  instead of the argmax category. Config value, not hardcoded, and expected
  to move once the eval set produces a calibration table.
- **Asymmetric rule for `To-Action`.** Independently of the argmax, if
  `p(To-Action)` exceeds a second, much lower threshold (starting at 0.15),
  `INBOX` is retained. This is nearly free now that the full distribution is
  available, and it protects the only error class that costs anything: a
  missed bill. A Receipts/Bookings coin-flip still just picks one and
  archives, because both outcomes are identical in behaviour.
- **Fixed letter mapping, not randomised.** See the label-bias note below.
- **Temperature does not affect reported logprobs** on this stack — measured
  identical to three decimal places at 0.5, 1.0 and 2.0. Pinned at `1.0`
  anyway to document the intent, but nothing depends on it. Note that
  repeated identical calls vary in the fourth decimal place, so confidence is
  reproducible to about three decimals, not exactly — do not assert exact
  equality in tests.
- **No second-model fallback in v1.** Low confidence goes straight to
  Needs-Review rather than escalating to a stronger model. A deliberate cut.
- **Input consistency is a hard requirement.** The poller and the backfill
  must build the model's input from the same fields, fetched the same way —
  see Backfill for why.
- **Prompt input fields** (sender, subject, body truncation length) are tuned
  during build against the eval set rather than guessed here. The one
  constraint set in advance: the body budget is spent on `To-Action` signal.
  A bill's due date is routinely below the snippet cutoff, and that is the
  one category where being wrong actually costs something. Phase 0 measured
  the cost of body text at roughly +74% latency on the 8B going from 300 to
  2000 characters — affordable.

**Rejected in Phase 0: self-reported confidence.** The original design asked
the model to emit `{reasoning, category, confidence}` as schema-constrained
JSON, with `reasoning` first to ground the number. The schema mechanism works
exactly as claimed — 30/30 valid outputs, property order held every time — but
the confidence it produces is worthless. Measured as mean confidence when
right minus when wrong: `qwen2.5:3b` **−0.050** (inversely calibrated),
`llama3.2:3b` **0.000** (it emitted `0.900` for all nine emails, right and
wrong alike), `llama3.1:8b` **+0.017**. A 0.8 threshold would have caught
none of the errors. The renormalised token distribution on the same emails
gave gaps of 0.000 / 0.332 / 0.209 — a real signal on two of three models.

**Rejected in Phase 0: self-consistency sampling.** Running N samples and
measuring agreement is a legitimate calibration technique, but it costs N×
latency for a coarser, quantised signal that logprobs provide continuously in
a single pass. Kept only as a fallback if a future runtime cannot expose
logprobs.

**Known risk — label-position bias.** Models can favour a letter slot
independently of what sits in it, which would make "confidence" partly an
artifact of the alphabet. Phase 0 measured this by classifying identical
emails under three different letter→category mappings and checking whether
the predicted category stayed stable: `qwen2.5:3b` 5/11, `llama3.2:3b` 5/11,
`llama3.1:8b` **9/11**. The 3B models change their answer on half the emails
based on nothing but which letter a category sits at, which makes their
distributions untrustworthy regardless of how well-spread they look. The 8B's
two instabilities were both on genuinely ambiguous pairs (Updates/Promotions,
Bookings/Receipts), which is defensible behaviour rather than bias.

The mitigation, if a future model needs it, is **permutation averaging** —
classify under two or three mappings and average the distributions. It is not
used in v1 because it costs a multiple of the latency, and because three
passes on a 3B (12.6s) is slower than one pass on the 8B (9.9s) while
starting from a worse distribution. Permutation stability must be re-measured
whenever the model changes.

### Model selection

**Lead candidate: `llama3.1:8b`.** It is the only model tested that survives
all three Phase 0 screens — acceptable latency, a confidence signal that
separates right from wrong, and stability under letter permutation.

| Model | Latency (2000-char body) | Calibration gap | Permutation stability |
|---|---|---|---|
| `qwen2.5:3b` | 4.3s | 0.000 (saturated at 1.00) | 5/11 |
| `llama3.2:3b` | 4.2s | 0.332 | 5/11 |
| `llama3.1:8b` | 9.9s | 0.209 | **9/11** |

`qwen2.5:3b` is rejected outright: besides the saturated distribution, it
classified an energy bill reading "payment due 15 October" as `Receipts` with
`p(To-Action) = 0.000` — a false negative on the one category that matters,
with no probability mass left for the asymmetric rule to catch it.

This is a **lead candidate, not a settled decision.** It rests on eleven
synthetic emails, and the ranking changed twice as each new screen was added
(speed, then calibration, then robustness). Phase 2 decides it against 150–200
real hand-labeled messages, and must re-measure all three properties rather
than inheriting these numbers.

### Error handling

Critical distinction: **a failure is not a low-confidence result.**

- **Low confidence** = a successful classification the model is unsure about
  → `Agent/Needs-Review` + `Agent/Processed`, keeps `INBOX`. Resolved by me.
- **Failure** = Ollama unreachable, malformed JSON, invalid category name,
  or a Gmail API error → **apply no labels at all**, including *not*
  `Agent/Processed`. The message stays invisible to the system and is
  naturally re-attempted on the next run.

Conflating these would let transient infrastructure problems permanently
pollute the review queue with mail that was never actually classified.

Additional rules:
- Retry a failing message a few times (small delay between attempts) before
  giving up and moving on to the next one.
- A single message's failure must never halt a poll cycle or a backfill run.
- **Auth failure is the exception to that rule.** An expired refresh token
  (`invalid_grant`) is not a per-message problem and retrying it accomplishes
  nothing — every subsequent message will fail identically. It halts the run,
  sets `needs_auth`, and surfaces in the webapp. See Authentication & re-auth.
- Log failures to stdout and to the classification log.

**Poison-pill handling.** "Never mark a failure as Processed" means a message
that *reliably* breaks the classifier — pathological encoding, an oversized
body, a prompt the model always fails on — is retried on every poll cycle
forever, and guarantees a backfill can never reach 3000/3000. After a
configurable number of cumulative failures (tracked in the classification
log, keyed by message id), the message gets a dedicated `Agent/Error` label
plus `Agent/Processed`, and is skipped from then on. It deliberately does
*not* go to Needs-Review: Needs-Review means "the model was unsure",
`Agent/Error` means "the system could not process this", and keeping those
separate is the whole point of the distinction above. `Agent/Error` is
surfaced in the webapp and should be empty; anything landing there is a bug
to investigate, not mail to re-file.

### Needs-Review reconciliation

When I manually re-file a message out of Needs-Review, nothing tells the
system. The message already carries `Agent/Processed`, so the agent never
looks at it again, and `Agent/Needs-Review` stays on it forever — meaning the
review queue only grows and "unresolved" stops meaning anything.

Each poll cycle therefore runs a cheap reconcile step before classification:

- Query `label:Agent/Needs-Review` and find messages that *also* carry a real
  category label
- Remove `Agent/Needs-Review` from them
- Write a **correction record** to the classification log: message id, what
  the model originally predicted (and at what confidence), what I chose
  instead

Those correction records are the highest-value data the system produces.
They're the prompt-tuning worklist in the Metrics view, and later they're the
seed corpus for retrieval-augmented few-shot classification. Without this
step, the correction event is unobservable and the model's original
prediction is lost the moment I re-file — the v2 RAG path would have nothing
to retrieve.

### Classification log (JSONL)

Every classification attempt appends one JSON object to a local `.jsonl`
file: timestamp, message id, sender, subject, source (`model` /
`prefilter` / `correction`), predicted category, confidence, reasoning,
action taken (labels added/removed, or `dry_run`), and any error.

This is **not** a database and doesn't undermine the no-DB decision below —
it's an append-only file with no schema, no migrations, and no queries beyond
"read it all and aggregate", which is trivial at this volume. It exists
because three things in this design are impossible without it:

- **Threshold tuning.** Gmail labels don't retain confidence or reasoning, so
  once a label is applied the evidence is gone. Nothing can be tuned from
  label counts alone.
- **Dry-run inspection.** A dry run writes no labels by definition, so the
  log is the *only* record a dry run produces.
- **The correction corpus.** See reconciliation above.

Rotate by size or date. Volume is trivial (a few thousand rows for the full
backfill, then tens per day).

### Evaluation set

A one-off manual exercise, done **before** the first live run: hand-label
150–200 real emails, sampled across all five categories, and commit the
result (message ids and my labels — not message content) as
`eval/labeled.jsonl`. A script scores the current model + prompt against it.

Outputs:
- Overall accuracy and a confusion matrix
- **Recall on `To-Action`** — the metric that actually matters, per the
  asymmetry noted in the taxonomy section. Everything else is cosmetic
  filing; a missed bill is not.
- A **calibration table**: predictions bucketed by reported confidence
  (0.5–0.6, 0.6–0.7, …) against measured accuracy in each bucket. This is
  what tells me whether 0.8 is a meaningful threshold or a decorative number.

Why this is in v1 and not deferred: it's roughly an hour of work, it's the
only thing that turns "I built a classifier" into "I built a classifier that
is 91% accurate with 97% recall on the category that matters", and it's the
regression check that makes it safe to change the prompt or swap the model
later. It also de-risks the first backfill more than dry-run alone does.

### Backfill (existing mail)

- Triggered manually via a **"Run Backfill" button** in the webapp — not
  automatic, not on first run.
- **Scope: all mail, not just the inbox.** Archived-but-unprocessed mail is
  included; reprocessing it is acceptable and desirable. Query drops the
  `in:inbox` constraint but keeps the rest:
  `-label:Agent/Processed -in:sent -in:drafts -in:chats`
- **Excluding sent/drafts/chats is not optional here.** Without `in:inbox`
  narrowing the search, the query otherwise sweeps in my own outgoing mail
  and labels it as `Promotions`. The ~3000 estimate should be re-measured
  with the exclusions applied.
- Uses the **same `classify_and_label()` function** as the live poller — the
  only difference is the message source. No duplicated classification logic.
- **Same fetch format as the poller: `format=full`.** The earlier plan to use
  `format=metadata` for backfill would have meant the two paths classify on
  different inputs (metadata returns headers and snippet but no body), so
  accuracy measured during backfill wouldn't transfer to live operation, and
  the eval set would only be valid for one of the two paths. There's also no
  quota argument for it: `messages.get` costs 5 quota units regardless of
  format, so `metadata` saves bandwidth, not quota.
- Idempotent via `Agent/Processed` — safe to stop and re-run.
- **`batchModify` requires grouping and costs error granularity.**
  `batchModify` applies *one* set of label changes to up to 1000 message ids,
  so messages must be bucketed by (labels added, labels removed) — in
  practice, one call per category — rather than one call per message. It also
  returns no per-message result, so a partial failure can't be attributed to
  a specific message, which conflicts with the per-message retry rule above.
  Resolution: classify per message and accumulate results in memory, then
  flush writes per category bucket at the end of each page. If a bucket write
  fails, retry the whole bucket; nothing in it was marked `Processed`, so
  re-running is safe.
- Rate limits are not the bottleneck. Gmail allows 250 quota units per user
  per second (≈50 message fetches/sec), so 3000 fetches is about a minute.
  Serial local inference at a few seconds per message is the real constraint
  — expect a multi-hour backfill and design the progress UI for that, not for
  a spinner.
- Runs as a resumable background task with progress ("1400/3000") surfaced in
  the webapp — not a blocking synchronous call.
- **Should be run in dry-run mode first**, then inspected via the Metrics
  view before enabling writes.

### Concurrency

The live poller and the backfill job draw from the same unprocessed pool and
could double-handle a message if run simultaneously — producing two different
category labels on one message. They also share a single local model, so
running both concurrently just contends for the same serial resource.

V1 solution: **a real lock, enforced server-side.** A module-level state enum
(`IDLE` / `POLLING` / `BACKFILLING`) checked inside the endpoint handlers,
guarded by an `asyncio.Lock`. Requests that would violate it are rejected
with a clear error.

Disabling the buttons in the UI is presentation on top of that, not the
mechanism — a page refresh, a second browser tab, or a `curl` to the endpoint
ignores a disabled button entirely.

### No database in v1

Message state lives entirely in Gmail labels:
- "Unprocessed" = missing `Agent/Processed`
- "Unresolved" = has `Agent/Needs-Review`
- "Broken" = has `Agent/Error`

A deliberate simplification — state Gmail already tracks for free doesn't
need duplicating locally, and there's no join, no transaction, and no
concurrent writer to justify a schema.

What *is* kept locally is the append-only classification log, because Gmail
labels don't retain confidence or reasoning and that evidence is needed for
tuning. A JSONL file is the smallest thing that solves that. The point at
which it stops being enough — when the correction corpus needs similarity
search rather than sequential scanning — is exactly the point where the
vector store in Future Enhancements arrives.

### Webapp & control flow

- FastAPI backend + simple single-page frontend, same app as the agent logic.
- **Auto-starts idle at login** (launchd/systemd/Task Scheduler user service)
  — dashboard available whenever the laptop is open, no terminal needed.
- **Does not auto-start the live poller.** The loop only activates on "Start"
  in the webapp — explicit control over when something with write access to
  the inbox is running. "Stop" halts it; closing the laptop halts it too.
- **Start validates credentials before doing anything else.** If the refresh
  token is dead, Start does not launch a poller that will fail on every
  cycle — it returns `needs_auth` and the UI sends me straight into the
  consent flow.
- **Config / Settings view**, backed by a config file:
  - Poll interval (default: 1 minute)
  - Confidence threshold (default: 0.8)
  - Ollama model in use
  - Dry-run mode toggle
  - Max failures before `Agent/Error`
- **Config is held in memory as the single source of truth**, written through
  to disk on save — not re-read from disk each poll cycle. Re-reading per
  cycle races with the Settings page mid-write and can load a half-written
  file. Toggling `dry_run` while a backfill is running is rejected outright:
  a run that is half dry and half live is not something I want to have to
  reason about afterwards.
- **Run Backfill** button with progress display, locked out while the poller
  runs (see Concurrency).
- **Status view:** agent running or not, last poll time, recent actions,
  current `Agent/Error` count, Gmail connection state and days until token
  expiry.
- **Metrics view** (below).

### Authentication & re-auth

The OAuth flow lives in the webapp as ordinary routes, which is why the
client is registered as a **Web application** rather than a Desktop app. The
installed-app helper (`run_local_server()`) blocks, starts a second web
server of its own, and shells out to a browser — all awkward inside a FastAPI
request handler, and a poor fit for something that now runs weekly rather
than once.

- `GET /auth/start` — builds the Google consent URL and redirects to it
- `GET /auth/callback` — receives the code, exchanges it for tokens, writes
  them to `data/`, records the consent timestamp, redirects to the dashboard

Re-auth is therefore an ordinary web login: click, approve at Google, land
back on the dashboard connected. First-time setup and weekly re-auth are the
**same code path**, so there's no separate CLI bootstrap script to maintain.

Four behaviours make the expiry non-silent:

1. **Start gate** — Start checks credential validity first and returns
   `needs_auth` instead of launching a doomed poller.
2. **Reconnect Gmail button** — always present in the status view, for
   fixing it before it bites.
3. **Proactive warning** — the consent timestamp is recorded at
   `/auth/callback`, and since the window is a known 7 days the dashboard
   shows "Gmail access expires tomorrow — reconnect" from day 6. Turns a
   weekly surprise into a warning.
4. **Reactive catch** — a mid-cycle `invalid_grant` stops the poller, sets
   `needs_auth`, and logs it, rather than retrying every minute for a day.

The 7-day figure is an assumption to confirm empirically, since the
proactive warning depends on it.

### Metrics view

One page, reading two files (`eval/labeled.jsonl` and the classification
log). No database.

**From the eval set — is the classifier any good?**
- Overall accuracy and confusion matrix
- Recall on `To-Action`, called out separately as the headline number
- The calibration table (confidence bucket → measured accuracy), which is
  what makes the 0.8 threshold defensible or exposes it as noise
- **Calibration gap** — mean confidence when right minus when wrong. A gap
  near zero means the confidence carries no information, whatever the
  accuracy says. This is the number that killed self-reported confidence in
  Phase 0 and it stays a first-class metric.
- **Permutation stability** — the fraction of eval messages whose predicted
  category is unchanged under a different letter→category mapping. Guards
  against the distribution being an artifact of label position rather than
  content.

**From the live log — what is it actually doing?**
- Volume per category over time; % of messages hitting Needs-Review;
  failure and `Agent/Error` counts
- Recent classifications with the full six-way probability distribution
  shown, not just the winning score — this is also what makes a dry run
  inspectable, and the runner-up is often the interesting part

**The improvement surface — what should I fix next?**
- The list of corrections from reconciliation: what the model predicted, at
  what confidence, versus what I chose. Sorted by confidence descending, so
  confidently-wrong predictions surface first — those are the prompt bugs
  worth fixing. This list is the v1 tuning worklist and the v2 few-shot
  corpus.

### Handling `To-Action` items day-to-day

Once an item is dealt with, remove `INBOX` from it manually (Gmail's archive
action). The `Agent/To-Action` label persists, so `label:Agent/To-Action`
remains a full searchable history of everything ever flagged. Pin that label
in the Gmail sidebar for quick access.

Known limitation: that view mixes still-pending and already-handled items
with no visual distinction. If that becomes annoying, an `Agent/Actioned`
label (as a second tag, not a destination) would allow
`label:Agent/To-Action -label:Agent/Actioned` for outstanding items only.
Not built preemptively.

---

## Technical Stack

- **Backend:** Python, FastAPI
- **Gmail access:** `google-api-python-client` + OAuth. **Web application**
  client type — *not* Desktop/installed-app — with redirect URI
  `http://localhost:8000/auth/callback` (Google permits plain HTTP for
  localhost). `gmail.modify` scope, required for label changes.
- **LLM inference:** Ollama, local model (assumption: `llama3.1:8b` or
  `qwen2.5:7b-instruct` — confirm once hardware is tested), called with
  schema-constrained structured output
- **Frontend:** Plain HTML/JS single page to start
- **Config:** single JSON file on disk, loaded once at startup into an
  in-memory object, written through on save from the Settings page
- **Persistence:** append-only JSONL classification log. No database, no
  vector store in v1
- **Dependencies:** `pyproject.toml` + a lockfile, installed into a venv
- **Process lifecycle:** OS login item for the FastAPI server; in-app
  Start/Stop for the poll loop

### OAuth: staying in Testing, and what that costs

A Google Cloud project whose consent screen is left in **Testing** status
issues refresh tokens that expire after **7 days**. Publishing to Production
removes that, but publishing demands full branding — logo, app domain,
privacy policy, terms of service — and, for Gmail's restricted scopes, likely
verification. None of that is worth doing for a single-user personal tool.

**Decision: stay in Testing and accept weekly re-authentication.** I'm the
only test user; the cost is one browser round-trip a week.

The danger isn't the re-auth itself, it's that the default failure mode is
*silent*: the token dies, the poller starts erroring inside a background
loop, and nothing surfaces until mail visibly piles up. So the expiry is
handled as a first-class application state rather than an error — see
**Authentication & re-auth** under Webapp & control flow.

Two tokens are involved and only one is a problem:
- **Access token** (~1 hour) — refreshed silently by `google-auth` on every
  call. Never visible.
- **Refresh token** (7 days in Testing) — cannot be renewed programmatically.
  Re-consent requires a human in a browser, so genuinely automatic recovery
  is impossible. Automatic *detection* plus one-click repair is the goal.

Expect the "unverified app" warning on **every** re-consent, not just the
first (Advanced → Go to email-agent). Unavoidable in Testing.

### Secrets & config hygiene

- `data/` is gitignored. Commit **`config.example.json`** at the repo root so
  a fresh clone has a template.
- OAuth client credentials and the stored token file must **never** be
  committed — both live in gitignored `data/`, documented in the README.
- `eval/labeled.jsonl` stores **message ids and my labels only**, never
  message content, so it's safe to commit.

---

## Repo Structure

Single repo — the agent is not a separate service, it's a background task
inside the same FastAPI app the webapp uses, sharing the Gmail client, Ollama
client, and config. Splitting into multiple repos would mean duplicating
shared code for no benefit at this scale.

```
email-agent/
  app/
    main.py             # FastAPI app + routes ONLY — thin, no logic
    agent.py            # poll loop, classify_and_label(), reconcile step
    rules.py            # pure decision logic: (category, confidence) -> labels
    classifier.py       # Ollama calls, prompt templates, category defs
    auth.py             # OAuth routes, token storage, credential validation
    gmail_client.py     # label ops, message fetching, batchModify
    prefilter.py        # sender-domain allowlist matching
    logbook.py          # append/read the JSONL classification log
    config.py           # load/save config, defaults, in-memory state
    backfill.py         # paginated sweep, resumable, progress tracking
    metrics.py          # aggregate log + eval results for the Metrics view
  frontend/             # dashboard: status, start/stop, settings, backfill, metrics
  eval/
    labeled.jsonl       # hand-labeled eval set (message ids + labels, no content)
    run_eval.py         # score current model+prompt; accuracy, confusion, calibration
  scripts/
    setup_labels.py     # idempotent label creation (check-then-create)
    spike_ollama.py     # Phase 0: latency, schema compliance, confidence method
    spike_labels.py     # Phase 0: label-position bias, temperature
    undo_run.py         # strip Agent/ labels, restore INBOX — time-window REQUIRED
  data/                 # config.json, oauth token, classifications.jsonl — gitignored
  tests/
  docs/
    PLAN.md             # phased build order and gates
  config.example.json
  pyproject.toml
  DESIGN.md
  README.md
```

Two structural decisions worth stating, since the point of this repo is that
I can explain all of it:

- **`agent.py` exists so `main.py` stays thin.** The poll loop and the shared
  `classify_and_label()` need an owner. Without one they drift into
  `main.py`, which becomes the 600-line file that nobody can follow.
- **`rules.py` exists to keep the decision logic pure.** The
  category+confidence → labels mapping is the single most testable thing in
  the project, and it only stays that way if it lives apart from the Ollama
  and Gmail I/O. Putting it inside `classifier.py` next to network calls
  would quietly make it untestable.

**Label creation must be idempotent** — Gmail errors when creating a label
that already exists, so `setup_labels.py` checks before creating and is safe
to re-run.

---

## Testing Strategy

Focused, not exhaustive — enough to catch real regressions without becoming a
maintenance burden on a solo project. Note the split: **tests** cover logic
correctness, the **eval set** covers model quality. They're different
questions and shouldn't be conflated.

**Worth testing:**
- Label decision logic in `rules.py`: given a distribution + thresholds,
  which labels get applied and is `INBOX` removed? Includes the asymmetric
  `p(To-Action)` rule firing even when the argmax is a different category.
  (Pure function, test it thoroughly.)
- Distribution renormalisation: letters outside the top-20 treated as zero,
  normalisation summing to 1, argmax selection, and the case where no valid
  category letter appears at all. Also pure, also worth testing thoroughly.
- Classifier output parsing: missing `logprobs` in the response, an empty
  top-20, whitespace-prefixed token variants (`"A"` vs `" A"`), confidence
  at/above/below threshold. Assert approximate equality on probabilities —
  repeated calls vary in the fourth decimal.
- Pre-filter matching: domain allowlist hits and misses
- Dry-run mode: asserts that **no write calls** are made — the most valuable
  single test here, given the blast radius of a bad backfill
- Reconciliation: a message with both `Needs-Review` and a real category has
  `Needs-Review` stripped and a correction logged
- Poison-pill: after N failures, `Agent/Error` + `Agent/Processed` are
  applied and the message is skipped thereafter

**Not worth testing:** the Gmail API client itself, Ollama's behaviour, or
the frontend. Gmail and Ollama calls get mocked; live behaviour is verified
by hand via dry-run against the real inbox, and model quality is measured by
the eval set rather than asserted in tests.

---

## Future Enhancements (out of scope for v1)

### Classifier quality
- **Permutation averaging** — classify each message under two or three
  letter→category mappings and average the distributions, cancelling out
  label-position bias. Measured as unnecessary for the chosen model in Phase
  0 (9/11 stable) and it costs a multiple of the latency, but it is the
  mitigation to reach for if a future model shows bias.
- **Semantic labels instead of letters** — possibly better grounded than
  arbitrary letters, but multi-token first tokens and prefix collisions
  (`Receipts`/`Reminders`) make the distribution hard to read. Only worth
  revisiting if letters prove to be the accuracy bottleneck.
- **Claude API fallback** — a second-tier check for low-confidence emails
  before giving up to Needs-Review (Ollama → Claude → Needs-Review). Cut from
  v1 to keep the escalation path simple to reason about first.
- **Retrieval-augmented few-shot classification** — embed the correction
  records from the classification log into a local vector store (Chroma + an
  Ollama embedding model) and retrieve similar past corrections as dynamic
  few-shot examples, layered on top of the static category definitions used
  in v1. The v1 reconciliation step exists specifically to make this
  possible.
- **Auto-growing sender allowlist** — suggest new promotional domains based
  on repeated corrections.

### Genuinely agentic versions
Both of these replace the fixed pipeline with a loop where the *model*
chooses the next action. Neither is warranted in v1, and both are worth
building later for the reasons given.

- **Triage agent with tools** — instead of one shot at a truncated body, give
  the model tools (`get_full_body`, `search_mail`, `get_thread`,
  `get_sender_history`) and let it decide what evidence it needs. An
  ambiguous message resolves as: fetch the body → search past mail from this
  sender → see I filed the last three as Receipts → classify accordingly. A
  clear message still resolves in one step with no tool calls. This is where
  the pipeline stops being the right answer — when classification needs to
  *gather evidence* rather than judge fixed input. It also solves two
  problems v1 just absorbs: body truncation losing a deadline, and having no
  way to use my own filing history.
- **Self-improving classifier loop** — an agent that reads the correction
  records, proposes a change (prompt edit, allowlist addition, sharpened
  category definition), scores it against the eval set, keeps it if accuracy
  improved and reverts if not, and repeats until it stops finding gains.
  Model-chosen actions, feedback from real measurements, model-decided
  termination. Only possible because v1 builds the eval set and the
  correction log first — which is the argument for building them now.

### Infrastructure
- **Event-driven trigger via Gmail Pub/Sub** — replace polling with push
  notification + pull subscription, removing the fixed-interval check.
  Polling ships first because it's simpler to build and debug; Pub/Sub is a
  deliberate v2 upgrade, not a missing requirement.
- **Thread-aware classification** — inherit an existing thread's `Agent/`
  label for new replies rather than reclassifying, and/or re-evaluate a
  thread's category when a reply changes its nature (a booking thread that
  later requests payment).
- **Remote/mobile access** — expose the webapp over Tailscale for phone
  access without public exposure; a thin mobile client is a further stretch.
- **Wake-triggered auto-start** (vs. login-only) — catching lid-open/wake
  events is more OS-specific and flakier; not needed for v1.

### Product
- **"Chat with your inbox"** — RAG-based search/Q&A over archived mail, built
  on the same vector store as the few-shot system.
- **`Agent/Actioned` label** — if distinguishing handled from pending
  `To-Action` items becomes necessary.
- **Re-evaluate `Agent/Updates`** — it and `Promotions` produce identical
  behaviour, so if the distinction isn't earning its keep, merging them costs
  nothing operationally.

---

## Open Assumptions to Confirm During Build

- **Final model choice.** `llama3.1:8b` leads on eleven synthetic emails,
  but the ranking changed twice during Phase 0 as new screens were added.
  Phase 2 re-measures latency, calibration gap and permutation stability
  against real hand-labeled mail and decides.
- **Where the confidence threshold actually belongs.** 0.8 is a starting
  value, not a measured one. The calibration table sets it. So does the
  `p(To-Action)` floor, provisionally 0.15.
- Prompt input fields and body truncation length — tuned against the eval
  set, with the body budget prioritised for `To-Action` signal.
- Initial seed list for the Promotions sender allowlist.
- **Whether `Personal` holds up on real mail.** It classified cleanly on
  three synthetic examples, but real human correspondence is far more varied
  than a note from a friend — forwarded threads, mailing lists, and
  recruiters all blur the line with `Updates`.
- Real backfill volume once sent/drafts/chats are excluded (~3000 is a
  pre-exclusion guess).
- **Exact refresh-token lifetime in Testing status** — assumed 7 days.
  Confirm empirically, since the proactive expiry warning is timed off it.
