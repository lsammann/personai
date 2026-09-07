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
- `scripts/oauth_setup.py` — installed-app OAuth flow, **`gmail.readonly`
  scope**, token stored in `data/`
- A throwaway script that fetches 20 messages and prints sender/subject/snippet
- A throwaway script that calls Ollama with a schema-constrained prompt on a
  hardcoded email and prints the parsed JSON

**Spike questions to answer and write down:**
- Does the consent screen need publishing to Production to avoid the 7-day
  refresh token expiry? *Test this by checking the granted token's behaviour,
  not by trusting the docs.*
- Which model runs comfortably here — `llama3.1:8b`, `qwen2.5:7b-instruct`,
  something smaller? **Time a single classification.** That number times 3000
  is the backfill duration, and if it's 15 seconds this project needs a
  different model before anything else gets built.
- Does Ollama's `format` schema actually constrain output the way `DESIGN.md`
  claims — including holding the property order so `reasoning` precedes
  `confidence`?

**Gate:** I can read my own mail from Python, and get valid structured JSON
out of a local model in a tolerable amount of time. Model choice is decided
(provisionally) and written into `DESIGN.md`'s open-assumptions section.

**If the gate fails:** a too-slow model means dropping to a smaller one or
reconsidering local inference entirely — that's a v1-scope conversation, and
much better to have now than after the webapp exists.

---

## Phase 1 — Pure core

**Goal:** everything that can be built and fully tested without touching a
network. This is where the "testable pure function" claim in `DESIGN.md` gets
earned rather than asserted.

**Build:**
- `app/rules.py` — `(category, confidence, threshold) -> (labels_to_add,
  labels_to_remove)`. The whole decision table, no I/O.
- `app/config.py` — load/save, defaults, in-memory source of truth
- `app/logbook.py` — append/read the JSONL classification log
- `app/prefilter.py` — sender-domain allowlist matching
- `app/classifier.py` — **split the parsing from the calling.** A pure
  `parse_response(raw: str) -> Classification` that can be tested against
  malformed JSON, missing fields, and out-of-enum categories without Ollama
  running.
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
- `eval/run_eval.py` — score the current model + prompt against the set.
  Outputs overall accuracy, confusion matrix, **recall on `To-Action`**, and
  the confidence-bucket calibration table.

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
3. **Does the calibration table show confidence tracking accuracy?** If every
   prediction comes back at 0.9+ regardless of correctness, the threshold
   mechanism is decorative, `Needs-Review` will sit empty, and Phase 3 needs
   a different low-confidence signal (self-consistency across two samples, or
   a runner-up category) before it's safe to let anything remove `INBOX`.

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

- **Phase 0, model speed.** Could force a smaller model, or reopen local-vs-API.
- **Phase 2, calibration.** If confidence doesn't track accuracy, the 0.8
  threshold is fiction and the low-confidence signal needs replacing. This is
  the most likely design change in the whole plan.
- **Phase 2, taxonomy.** The confusion matrix may show `Updates` and
  `Promotions` are indistinguishable in practice — at which point merging
  them costs nothing, since they behave identically.
- **Phase 4, backfill volume.** ~3000 is a guess made before excluding
  sent/drafts/chats. Re-measure.

Update `DESIGN.md` when these resolve. A design doc that still describes the
plan rather than the thing is worth less in an interview than one whose
assumptions section shows what got tested and what changed.
