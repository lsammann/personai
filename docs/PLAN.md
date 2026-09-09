# Build Plan — Email Agent v1

Companion to `DESIGN.md`. That doc says *what* to build and why; this one says
*in what order* and *what has to be true before moving on*.

---

## Ordering principle

Two rules decide the sequence, and they conflict with the instinct to build
the fun part first:

**1. Kill the environmental risks before writing real code.** Two things could
sink this project and neither is about my code: OAuth might not behave (see
the 7-day refresh token trap), and the local model might be too slow or too
dumb on this hardware. Both are cheap to test and expensive to discover in
week three. They go first.

**2. Nothing gets write access to the inbox until it has been measured and
the undo path exists.** The agent removes `INBOX` from mail in bulk. That's
the whole risk surface of this project. So the order is: prove it reads →
prove it classifies well → prove I can undo it → *then* let it write.

A consequence worth stating explicitly: **the eval set gets built before the
writer.** That feels backwards — building a measurement harness for a thing
that doesn't exist yet — but classification quality is the go/no-go for
whether this is worth pointing at a real inbox at all, and finding out it's
60% accurate is much cheaper before I've built the backfill than after.

### Why not a walking skeleton?

The usual advice is to build a thin end-to-end slice first and thicken it.
I'm not doing that here, deliberately: an end-to-end slice means writing to
the inbox on day one, before dry-run, before the undo script, and before any
idea of whether the model is any good. The risk profile of this project
doesn't match the risk profile that walking-skeleton advice assumes. Phases
0–2 are all read-only by construction.

### Safety mechanism: start on a read-only OAuth scope

Phases 0–2 authenticate with **`gmail.readonly`**. Not `gmail.modify`.

This makes damaging the inbox *structurally impossible* for the first half of
the build, rather than merely unlikely — a bug in a fetch loop can't remove
`INBOX` from anything if the token doesn't carry the permission. Upgrading to
`gmail.modify` in Phase 3 forces a re-consent, which is a useful, deliberate
moment: that's the point where the project acquires the ability to do harm,
and it should feel like a step rather than a default.

---

## Phase overview

| Phase | Name | Scope | Risk retired |
|---|---|---|---|
| 0 | Foundations & spikes | read-only | Does OAuth work? Is the model fast enough? |
| 1 | Pure core | no I/O | Is the decision logic correct? |
| 2 | Eval harness | read-only | Is the classifier actually any good? |
| 3 | The writer | **write** | Can I safely modify mail — and undo it? |
| 4 | Automation | write | Does it run unattended? |
| 5 | Webapp | write | Can I control and observe it? |
| 6 | Ship it | write | Does it survive a reboot? |

Rough sizing: Phase 0 an evening, 1 an evening, 2 the longest single chunk
(the manual labeling is the cost), 3 an evening, 4 two, 5 two or three, 6
short. Estimates on a personal project are fiction, but the *relative* sizes
are about right — and Phase 2 being the biggest is the point, not a problem.

---

## Phase 0 — Foundations & spikes

**Goal:** prove the two things I don't control actually work, before building
anything on top of them.

**Build:**
- Repo scaffold: `pyproject.toml`, `uv` venv, `.gitignore` (`data/` first
  line), `README.md` stub, the directory tree from `DESIGN.md`
- `app/auth.py` — OAuth routes on the **`gmail.readonly` scope**, token
  stored in `data/`
- A throwaway script that fetches 20 messages and prints sender/subject/snippet

The spikes live in `scripts/spike_ollama.py`, `scripts/spike_labels.py`
and `scripts/spike_gmail.py`.

### Findings

**Speed is not a constraint.** Measured on a Ryzen 5 PRO 7540U, CPU-only,
on mains power. Earlier estimates of 25–50 hours were wrong by roughly 4×.

| Model | 300-char body | 2000-char body | Backfill (3000) |
|---|---|---|---|
| `qwen2.5:3b` | 2.8s | 4.3s | 2.3–3.6h |
| `llama3.2:3b` | 2.7s | 4.2s | 2.3–3.5h |
| `llama3.1:8b` | 5.7s | 9.9s | 4.7–8.2h |

Nothing is eliminated on latency. Even the slowest configuration is a single
overnight backfill and a ~33-minute eval run, so prompt iteration in Phase 2
is practical. Body text costs +55% latency on 3B and +74% on 8B — affordable,
which means the `To-Action` recall argument for feeding in real body content
survives.

**Schema-constrained output works exactly as `DESIGN.md` claimed.** 30/30
valid JSON, zero invalid category names, and property order held in every
case. That assumption is settled — but it turned out not to matter, because
the confidence it produced was worthless.

**Self-reported confidence is dead.** Mean confidence when right minus when
wrong: `qwen2.5:3b` −0.050, `llama3.2:3b` 0.000, `llama3.1:8b` +0.017.
llama3.2 emitted `0.900` for all nine test emails, right and wrong alike. A
0.8 threshold would have caught none of the errors. Replaced with the
renormalised first-token distribution — see `DESIGN.md` → Classification
logic.

**Label-position bias is real and disqualifies the 3B models.** Classifying
identical emails under three different letter→category mappings, the
predicted category stayed stable for `qwen2.5:3b` 5/11, `llama3.2:3b` 5/11,
`llama3.1:8b` **9/11**. The 3B models change their answer based on nothing but
which letter a category sits at, so their distributions cannot be trusted —
including llama3.2's apparently healthy 0.332 calibration gap, which is better
explained as a flat, noisy distribution. The 8B's two instabilities were both
on genuinely ambiguous pairs, which is defensible rather than biased.

**Temperature does not affect reported logprobs** (identical to three decimals
at 0.5 / 1.0 / 2.0). Repeated identical calls do vary in the fourth decimal,
so confidence is reproducible to ~3dp, not exactly.

**A sixth category, `Personal`, was added.** Human correspondence fits none of
the five machine-mail categories. Relying on the confidence floor to catch it
failed in measurement — qwen classified a two-line note from a friend as
`Updates` at 0.953 and would have archived it silently.

**Lead candidate: `llama3.1:8b`** — the only model to survive all three
screens. Not settled: the ranking changed twice as screens were added, and it
rests on eleven synthetic emails I wrote. Phase 2 decides against real mail.

### Remaining spike question

- **Exact refresh-token lifetime in Testing status.** Assumed 7 days; the
  proactive expiry warning is timed off it. Confirm empirically once OAuth is
  wired up.

**Gate:** I can read my own mail from Python, and a local model returns a
usable, well-behaved category distribution in tolerable time.

---

## Phase 1 — Pure core

**Goal:** everything that can be built and fully tested without touching a
network. This is where the "testable pure function" claim in `DESIGN.md` gets
earned rather than asserted.

**Build:**
- `app/categories.py` — the six categories, their letter mapping, and the
  `Agent/` label names. Shared constants, so `rules` and `classifier` do not
  have to depend on each other.
- `app/rules.py` — `(distribution, config) -> (labels_to_add,
  labels_to_remove)`. The whole decision table including the asymmetric
  `p(To-Action)` rule. No I/O, no network — the most-tested file in the
  project.
- `app/config.py` — load/save, defaults, in-memory source of truth *(done in
  Phase 0; auth needed it)*
- `app/logbook.py` — append/read the JSONL classification log
- `app/prefilter.py` — sender-domain allowlist matching
- `app/classifier.py` — **split interpreting the model from calling it.** The
  pure half turns a raw top-20 logprob list into a normalised distribution
  over the six categories, and is testable against a partial top-20, tokens
  with leading whitespace, and a response containing no valid category letter
  at all. The impure half is the Ollama HTTP call, mocked in tests.
- `tests/` covering all of the above

**Gate:** `pytest` passes, and the test suite covers every row of the label
decision table plus every malformed-input case in `DESIGN.md`'s testing
section. No mocks needed yet, because there's no I/O to mock.

**Why here:** it's the cheapest phase, it's completely safe, and it means
Phase 2's iteration loop is only ever debugging *prompts*, never debugging
whether the parser is broken.

---

## Phase 2 — Eval harness

**Goal:** an accuracy number. This is the go/no-go gate for the whole project
and the single highest-value phase for interview purposes.

**Build:**
- `scripts/label_eval.py` — a small CLI that pulls N unlabeled messages,
  prints sender/subject/snippet, and prompts me to type a category. Writes
  `eval/labeled.jsonl` (message ids + my labels only, never content — so it's
  safe to commit).
- **Hand-label 150–200 messages.** Sample deliberately across categories
  rather than taking the most recent 200, which would be 80% promos.
  Confirmed and probably understated - twenty consecutive recent inbox
  subjects contained no To-Action, Personal, Receipts or Bookings at all.
  See `docs/BACKLOG.md`.
- `eval/run_eval.py` — score the current model + prompt against the set.
  Outputs overall accuracy, confusion matrix, **recall on `To-Action`**, the
  confidence-bucket calibration table, the **calibration gap**, and
  **permutation stability**.

**Re-run the Phase 0 screens against real mail.** The synthetic results are
directional only, and the model ranking already reversed twice under them.
All three properties get re-measured here — latency, calibration gap, and
stability under a permuted letter mapping — because a model that looked
stable on eleven emails I wrote may not be on two hundred of mine.

**Then iterate.** This is the real work of the phase: tune the prompt, the
category definitions, the precedence rule, the input fields, and the body
truncation length against a fixed measurement. Try the candidate models from
Phase 0 against each other. Every change is scored, not vibed.

**Gate — three questions, all answerable from the output:**
1. Is overall accuracy tolerable? (If the four archive-destined categories
   get confused with each other, that's cosmetic — see the binary action
   space argument in `DESIGN.md`.)
2. **Is recall on `To-Action` high?** This is the one that matters. A missed
   bill is the only error with a real cost.
3. **Is the calibration gap meaningfully positive?** If mean confidence when
   right is no higher than when wrong, the threshold is decorative,
   `Needs-Review` will sit empty, and nothing should be allowed to remove
   `INBOX` until a working signal exists. This is the screen that killed
   self-reported confidence in Phase 0.
4. **Is permutation stability high?** If the predicted category moves when
   only the letter mapping changes, the distribution is measuring alphabet
   position rather than content. Mitigation is permutation averaging, at a
   multiple of the latency.
5. **What are the actual threshold values?** 0.8 and a 0.15 `p(To-Action)`
   floor are placeholders. The calibration table replaces them with measured
   ones, and those go back into `DESIGN.md`.

**If the gate fails:** stop and fix it here. Everything downstream is
plumbing around a classifier; plumbing around a bad classifier is wasted
work, and this is the cheapest possible place to discover it.

---

## Phase 3 — The writer

**Goal:** first write access. Handled carefully.

**Build, in this order — the order is the point:**
1. `scripts/setup_labels.py` — idempotent label creation, all seven labels
   plus `Agent/Error`
2. **Re-auth to `gmail.modify`.** The project can now do damage.
3. **`scripts/undo_run.py` — before anything writes in anger.** Required
   time-window argument, no default. Test it by hand-applying `Agent/`
   labels to five throwaway messages and confirming it strips them cleanly.
4. `app/gmail_client.py` — fetch, and the single atomic `messages.modify`
   call
5. `app/agent.py` — `classify_and_label()`, honouring `dry_run`
6. A thin CLI entry point to run one pass over N messages. **Not the webapp
   yet** — a UI at this stage is a second thing that can be broken while I'm
   trying to establish whether the first thing works.

**Gate — a deliberate escalation, not one step:**
- Dry run over ~50 messages; read the log; the intended actions look right
- Live run over a *tiny* slice (`newer_than:1d`, or a dozen messages).
  Inspect the actual inbox.
- Run `undo_run.py` over that window. Confirm the inbox is exactly as it was.
- Only then is bulk processing on the table.

That undo rehearsal is not optional. An undo script that has never been run
is a plan, not a safety net.

---

## Phase 4 — Automation

> Read `docs/BACKLOG.md` first. The real backlog is 18,668 messages, not the
> ~3000 DESIGN.md assumes, which makes a single undifferentiated pass a
> ~49-hour job. That document sizes three strategies; none is chosen.

**Goal:** it runs by itself over everything.

**Build:**
- The poll loop in `app/agent.py`, with start/stop as function calls
- The Needs-Review reconciliation step
- `app/backfill.py` — pagination, resumability, per-category `batchModify`
  bucketing, progress counters
- Poison-pill handling: failure counts from the log, `Agent/Error` after N

**Gate:**
- **Full backfill in dry-run first.** Inspect the log via `run_eval`-style
  aggregates before enabling writes — this is the moment the eval set pays
  off a second time, because I can compare predicted category distribution
  against expectations.
- Then the live backfill. Expect hours; it's serial local inference.
- Poller left running for a day. Mail lands in the right places, `Needs-Review`
  is non-empty but not overwhelming, `Agent/Error` is empty.
- Manually re-file something out of `Needs-Review` and confirm the next cycle
  strips the label and writes a correction record.

---

## Phase 5 — Webapp

**Goal:** control and observability without a terminal.

**Build:**
- `app/main.py` — thin routes only: start, stop, status, config get/save,
  backfill start, progress, metrics
- The **server-side lock** (state enum + `asyncio.Lock`), enforced in the
  handlers
- `app/metrics.py` — aggregate the log and the eval results
- `frontend/` — status, start/stop, settings, backfill progress, and the
  Metrics view (eval numbers, live volumes, and the corrections worklist
  sorted by confidence descending)

**Gate:** every operation I've been doing by CLI is doable from the browser,
the lock genuinely rejects a concurrent backfill request sent by `curl` (not
just a greyed-out button), and the Metrics view tells me something I didn't
already know.

**Why last:** it's the least risky and least uncertain part of the build. It
wraps things that must already work, and building it earlier would mean
maintaining it through every change in Phases 3 and 4.

---

## Phase 6 — Ship it

- Login service (`systemd --user` on this machine) starting the server idle
- `README.md`: setup from scratch, OAuth steps, the consent-screen
  Production note, how to run a dry run, how to undo
- `config.example.json` committed
- Confirm survival across a reboot

---

## Deferred deliberately

Not in v1, tracked in `DESIGN.md`'s Future Enhancements: asymmetric
thresholds, Claude API fallback, RAG few-shot, Pub/Sub push, thread-aware
classification, the agentic triage loop, the self-improving loop, Tailscale
access.

**Rule for the build:** when one of these gets tempting mid-phase, write it
down and keep going. The eval set and the correction log are the things that
make all of them possible later, and they're both in v1 — the extension path
is already paid for.

---

## Checkpoints where the design might change

Places where reality is expected to argue back, and that's fine:

- ~~**Phase 0, model speed.**~~ *Resolved: not a constraint. 2.7–9.9s per
  message, every candidate viable.*
- ~~**Phase 0, calibration.**~~ *Resolved the hard way: self-reported
  confidence carried no signal on any model and was replaced with the
  renormalised token distribution. The design changed, as expected — this was
  correctly flagged as the most likely change in the plan.*
- **Phase 2, calibration and stability on real mail.** The Phase 0 numbers
  come from eleven synthetic emails. Both the model ranking and the threshold
  values are provisional until re-measured here.
- **Phase 2, the `Personal` category.** Clean on three synthetic examples;
  real human mail is messier — forwarded threads, mailing lists and
  recruiters all blur the line with `Updates`.
- **Phase 2, taxonomy.** The confusion matrix may show `Updates` and
  `Promotions` are indistinguishable in practice — at which point merging
  them costs nothing, since they behave identically.
- **Phase 4, backfill volume.** ~3000 is a guess made before excluding
  sent/drafts/chats. Re-measure.

Update `DESIGN.md` when these resolve. A design doc that still describes the
plan rather than the thing is worth less in an interview than one whose
assumptions section shows what got tested and what changed.
