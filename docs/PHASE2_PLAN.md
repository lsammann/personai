# Phase 2 — Eval harness

Companion to `docs/PLAN.md`, which says what Phase 2 contains and what has to
be true before Phase 3. This document says *how* it gets built.

> **Provenance.** Agreed in a planning session on 2026-09-09 that was lost
> before it could be written down. Recovered on 2026-09-10 from the Claude
> Code transcript on disk (`~/.claude/projects/<slug>/*.jsonl`, the
> `ExitPlanMode` record of session `1ee31ef6`). The body below is that plan,
> amended where the build has since diverged from it. Amendments are listed
> immediately below and marked **[amended 2026-09-10]** where they appear.

## Amendment log

**A1 — Body extraction split across two modules.** §1 originally put a single
`extract_text(payload) -> str` in `gmail_client` and a finished `body: str` on
`Message`. As built, `gmail_client` returns *both* text parts untouched and a
new pure module `app/message_body.py` owns HTML stripping and the plain-vs-HTML
choice. Reason: the selection rules are model-input policy, not transport — a
change to `STUB_MAX_CHARS` alters what the model sees on ~1.3% of mail exactly
as `body_chars` does, so it must sit where the eval harness can version it. A
knob living inside the transport adapter would be invisible to the harness
meant to control for it. See `app/message_body.py:1-23` and `DESIGN.md` →
Classification logic → Body extraction. §1 and §6 rewritten accordingly.

**A2 — `extraction_version` joins the run manifest.** Direct consequence of
A1: `EXTRACTION_VERSION` (`app/message_body.py:38`, currently `"v2"`) is now
one of the inputs that changes predictions, so two runs either side of a bump
are not comparable. It is recorded alongside `model`, `prompt_id` and
`body_chars`, and it belongs in the list of things that force re-inference.
§3 (Manifest) and §4 amended.

**A3 — `classify()` requires both text parts.** `text_html` briefly carried a
`""` default. Removed, so no caller can reach the model having supplied only
`text_plain` and silently classify HTML-only mail — 23% of the mailbox — on an
empty body. The default made a structural guarantee conventional. §5 unaffected;
noted here because it changes the signature the harness calls.

---

## Context

Phase 1 closed with 149 tests and a decision core that has never seen a real
email. Everything downstream of here is plumbing around a classifier whose
quality is unmeasured: `docs/PLAN.md` makes Phase 2 the go/no-go gate for the
project, and the three numbers that decide it — `To Action` recall, the
calibration gap, and permutation stability — currently rest on eleven
synthetic emails written in Phase 0, where the model ranking reversed twice as
screens were added.

The obstacle is sampling. `docs/BACKLOG.md` measured twenty consecutive recent
inbox subjects containing zero `To Action`, zero `Personal`, zero `Receipts`
and zero `Bookings`. A random sample of 200 would be almost entirely
promotional mail, so the headline metric would be computed over single-digit
examples. Over-sampling the rare categories fixes that and breaks the overall
accuracy number, which no longer describes the mailbox.

The outcome this phase produces:

- A committed, reproducible eval set of 200 hand-labelled messages
  (ids and labels only — never content)
- Two accuracy numbers, both honest: eval-set accuracy for tuning, and a
  weighted estimate of mailbox accuracy for reporting
- Measured values for `confidence_threshold` and `to_action_floor`, replacing
  the 0.8 / 0.15 placeholders in `DESIGN.md`
- A decided model, prompt and `body_chars`, each chosen against a fixed
  measurement rather than a preference
- A regression harness that makes every later prompt or model change scorable

---

## Decisions taken (from the planning discussion)

| Decision | Choice |
|---|---|
| Sampling frame | `newer_than:1y -in:sent -in:drafts -in:chats` (~6,300) |
| Rare-class mining | May reach beyond 1y, in its own stratum, excluded from the weighted estimate |
| Label budget | 200 + a 20-message blind recheck |
| Dev / holdout | 140 / 60, split at **score** time, not predict time |
| Eval predictions | `eval/results/<run_id>.jsonl`, not the classification log |
| `PROMPT_VERSION` | `int` → `str` prompt id (`"v3-letters"`), incl. `logbook.Record` |
| `v6-date` prompt variant | Dropped — the poller only ever sees new mail |
| Ground-truth rule | Label what it **should have been when it arrived** |

---

## 1. The read half — `app/gmail_client.py` + `app/message_body.py`

**[amended 2026-09-10 — see A1. Built and committed in `03110e6`.]**

Not script-local helpers. `DESIGN.md` states input consistency as a hard
requirement: if the eval harness extracts bodies differently from the agent,
the eval measures a pipeline that will never run. The MIME walk therefore
lives in shared code from the start, and Phase 3 adds `modify()` to
`gmail_client`.

What changed from the original plan is *where the line falls*. The original
had one function returning a finished body. As built there are two modules,
divided by a single question: **does this decision change what the model
sees?**

- **No — it's dictated by Gmail's wire format** → `gmail_client`. Fetching,
  base64 decoding, walking the MIME tree, retrying a rate limit.
- **Yes — it's a tuning knob** → `message_body`. Which text part to read,
  how to turn markup into words. Versioned by `EXTRACTION_VERSION`.

### `app/gmail_client.py` — transport

```python
@dataclass(frozen=True)
class Message:
    id: str; thread_id: str; sender: str; subject: str
    internal_date: dt.datetime
    text_plain: str; text_html: str        # both, decoded, no preference
    label_ids: tuple[str, ...]

def service(credentials: Credentials | None = None): ...
def search_ids(svc, query: str, limit: int | None = None) -> list[str]: ...
def count(svc, query: str) -> int: ...
def fetch(svc, message_id: str) -> Message: ...
def collect_text_parts(payload: dict) -> dict[str, str]: ...   # pure
def header(payload: dict, name: str) -> str: ...
def throttle(seconds: float): ...
```

The walk keeps the **longest** `text/plain` and `text/html` leaf rather than
the first, and appends an attached `message/rfc822` after the covering note in
a second pass, with a forwarded-message divider. Order is load-bearing:
truncation cuts from the start, and without the two passes a forwarded
original would beat a two-line note under longest-wins and displace it
entirely. Measured at 0 of 250 sampled messages, so this is consistency
between Gmail's two forward buttons rather than a fix to an observed failure.

**Reuse:** `extract_text`, `message_ids`, `count_query`, `header` and
`service` already existed in `scripts/spike_gmail.py:107-152` and were lifted;
`count` paginates rather than trusting `resultSizeEstimate`, which is the
behaviour the stratum sizes depend on. Auth comes from
`app.auth.load_credentials()`, unchanged and still `gmail.readonly`.

**Beyond the original plan:** retry with equal jitter on transient statuses,
and an opt-in `throttle`. Justified by the Gmail API quota section added to
`docs/BACKLOG.md` — a 200-message labelling sweep and a 6,300-message frame
enumeration both trip rate limits during normal operation, not just on
failure.

**Safety:** a source-level test asserts the module contains no `modify`,
`batchModify`, `trash` or `delete` call, mirroring the import-parsing purity
test guarding `app/decision.py`.

### `app/message_body.py` — model-input policy, pure

```python
BodySource = Literal["plain", "html", "stub_fallback", "none"]

@dataclass(frozen=True)
class Selection:
    text: str; source: BodySource; parsed_ok: bool

EXTRACTION_VERSION = "v2"
STUB_MAX_CHARS = 200
STUB_HTML_RATIO = 2.0

def strip_html(html: str) -> tuple[str, bool]: ...
def select_body(text_plain: str, text_html: str) -> Selection: ...
```

Measured over 150 messages from the eval frame
(`scripts/measure_bodies.py`, written up in `docs/BACKLOG.md` → Body
structure): 76.7% of mail has a real `text/plain` part, 23.3% is HTML-only,
1.3% carries a stub plain part with the real content only in the HTML, and
stripping tags keeps 3.2% of the HTML source.

Three things worth carrying forward into the harness:

- `source` is recorded per message, so the phase can ask whether accuracy
  differs on HTML-only mail. Unanswerable after the fact.
- `parsed_ok=False` returns the text parsed *before* the failure rather than
  `""`. Empty would make `select_body` see no HTML at all and fall back to a
  stub, turning a 90%-parsed message into a stub classification — and would
  drag the measured body-length distribution down.
- Stripping is a stdlib `HTMLParser` subclass. No dependency: `html2text` and
  BeautifulSoup buy table layout and link reference lists, all of which the
  truncation discards anyway.

---

## 2. `scripts/label_eval.py` — sampling and labelling

Four subcommands. Sampling and labelling are separated so the frame is fixed
and auditable, and so resumption is `sampled − labelled` rather than a
re-derived query.

```
label_eval.py sample  --seed 7 [--frame ...]   -> eval/sample.jsonl, eval/strata.json
label_eval.py label   [--stratum S_action] [--limit 25]
label_eval.py verify
label_eval.py recheck --n 20 --seed 99
```

```python
def build_sample(svc, frame_query: str, strata: list[Stratum], seed: int) -> SamplePlan: ...
def load_progress(sample: SamplePlan) -> tuple[list[str], set[str]]: ...
def render(message: Message, body_chars: int, show_full: bool) -> str: ...
def prompt_for_label(message: Message) -> LabelDecision | None: ...
def record(decision: LabelDecision, path: Path) -> None: ...
```

### Strata

Defined by **observable Gmail queries**, never by my guess at the label — a
stratum built from "emails I think are bills" leaks the label into the frame
and makes everything downstream uninterpretable.

| Stratum | Query | Purpose | n |
|---|---|---|---|
| `R` | the frame, uniform random | prevalence + representativeness | 80 |
| `S_action` | `subject:(invoice OR "payment due" OR overdue OR renew OR expires OR "action required" OR statement OR verify)` | mine `To Action` | 35 |
| `S_human` | threads containing `from:me`, plus `is:starred` | mine `Personal`/`To Action` **without keyword bias** | 35 |
| `S_txn` | `subject:(receipt OR order OR booking OR reservation OR confirmation)` | mine `Receipts`/`Bookings` | 30 |
| `S_other` | frame minus the above | promo bulk | 20 |

Disjointness is enforced by **ordered assignment**: enumerate the frame ids
once into `data/eval_frame.jsonl` (gitignored), then assign each id to the
first stratum it matches by set subtraction. That yields exact `N_h`, which
the weights need, and makes re-sampling reproducible without re-hitting the
API. `S_human` ids must be intersected back into the frame (sent messages are
not in it) or `N_h` is wrong.

If a rare stratum can't be filled inside 1 year, it extends further back as a
separate stratum with its own `N_h` measured over the wider query. Those
messages count toward `To Action` recall (conditional on true class, so still
valid) but are **excluded from the weighted mailbox estimate** — a 2022 promo
must not carry a weight derived from a population it wasn't drawn from.

### The labelling UI

Shows sender, subject, date, age, and the body: first ~1,500 chars with a
visible truncation marker, `m` expands to full.

**The marker is informational only.** Ground truth is what the *email* is, not
what the model can see — if it moved with `body_chars`, every truncation
change would silently redefine the target and `body_chars` would stop being
tunable. Stated in the CLI help, alongside the arrived-at rule: label what it
should have been **when it arrived**, not what it is today.

Keys: `1`–`6` category, `u` label-but-flag-unsure, `s` skip, `b` back (fix the
previous one — an unfixable misfire poisons ground truth), `m` more body,
`q` save and quit. Each decision is appended and flushed immediately; a crash
40 minutes in must cost zero labels.

### Schemas

`eval/sample.jsonl` — committed:
```json
{"message_id":"...","stratum":"S_action","split":"dev",
 "internal_date":"2026-03-11T...","seed":7}
```

`eval/labeled.jsonl` — committed:
```json
{"message_id":"...","label":"To Action","unsure":false,
 "labelled_at":"2026-09-10T...","pass":1}
```

Weights are **not** stored — derived in `run_eval` from `strata.json`'s
`N_h`/`n_h`, so a re-measured `N_h` can't leave stale weights on disk.

No sender, subject, body, snippet or domain. A unit test asserts the key set
of every line against an allowlist and fails on anything else — "never commit
content" is an invariant, so it gets a test rather than a docstring.

`data/eval_cache/<message_id>.json` — gitignored, written at label time, read
by every eval run. This is what makes the eval reproducible (refetching each
run makes the measurement depend on the mailbox's current state, and a deleted
message silently changes the denominator between two runs being compared),
offline, and fast enough to iterate against. It caches `text_plain` and
`text_html` — the raw parts, not a selected body, so that a bump to
`EXTRACTION_VERSION` can be re-scored against the same cache.

---

## 3. `eval/run_eval.py` — predict / score

The structural idea of the harness, mirroring the `interpret` / `classify`
split already in `app/classifier.py`:

```python
def predict(sample, labels, cache, *, model, body_chars, prompt_id,
            order=DEFAULT_ORDER) -> RunResult: ...      # slow, hits Ollama
def score(result: RunResult, *, confidence_threshold: float,
          to_action_floor: float, weights: Mapping[str, float],
          split: str = "dev") -> Report: ...            # pure, no network
```

Thresholds are **scoring-time** parameters. Consequences:

- Needs new inference: prompt, model, `body_chars`, letter order, and
  `EXTRACTION_VERSION` **[amended — A2]**.
- Free forever after: `confidence_threshold`, `to_action_floor`, a
  `retained_mass` floor, bucket widths, weighting, dev-vs-holdout — and any
  metric invented later, which re-scores every historical run retroactively.
- `score()` is a pure function over a JSONL file, so the whole report is unit
  testable with no mocks at all.

`predict` calls `classifier.classify(sender, subject, text_plain, text_html,
model=..., body_chars=...)` unchanged — no new classifier code is needed,
which retroactively validates the Phase 1 split.

### What `score` reports

1. **Accuracy** — unweighted (tuning signal) and weighted `w_h = N_h/n_h`
   (mailbox estimate), each with a Wilson interval. At n=140 that's roughly
   ±5 points; every comparison is read against it.
2. **Confusion matrix** 6×6, plus a collapsed **2×2 action matrix**
   (keeps-INBOX vs archived) — the matrix with actual consequences, per
   `DESIGN.md`'s binary-action-space argument.
3. **Two `To Action` numbers, named separately:**
   - `to_action_recall` — argmax was `To Action`
   - `to_action_retention` — kept `INBOX` after `decision.decide()`, counting
     the asymmetric floor and `Needs Review` as saves. This is the operational
     metric; a bill classified `Receipts` at `p(To Action)=0.22` is not a
     missed bill, but `DESIGN.md`'s metric as written scores it as one.
   Both broken out **by stratum of origin** — keyword-mined examples say
   "overdue" on the tin, so the honest number is the one from `R` + `S_human`.
   Misses printed to the terminal with sender/subject for inspection
   (terminal only, never into a committed file).
4. **Calibration table**, the **calibration gap**, and the **threshold sweep**
   over `T ∈ {0.5…0.95}` showing `Needs Review` rate, accuracy on the
   auto-applied remainder, and `To Action` misses among them. Then the `F`
   sweep at the chosen `T`, respecting `T + F < 1` — the coupling
   `app/config.py:_asymmetric_rule_must_be_reachable` already enforces.
5. **Permutation stability** — 2 extra *fixed* letter orders (rotation,
   reversal) built through `categories.letter_map(order)`, which is already
   parameterised for exactly this. Reports unchanged-argmax fraction and mean
   total-variation distance, a finer signal than a count at this sample size.
   Behind `--permutations`; it triples runtime, so it runs when choosing a
   model, not on every prompt tweak.
6. **`retained_mass`** — p1/p5/p10/p50, and accuracy conditioned on
   `retained_mass` decile. This answers the question `DESIGN.md` explicitly
   defers ("whether a floor on it should force Needs Review is a Phase 2
   question"). If the bottom decile isn't materially worse, the answer is *no
   floor*, recorded as such.
7. **Latency** — mean/p50/p90 at this `body_chars`. `docs/BACKLOG.md` says the
   Phase 0 figures are optimistic on real bodies; re-measuring is a phase task
   and it's free to collect here.
8. **Accuracy by `body_source`** **[amended — A1]**. `plain` / `html` /
   `stub_fallback`, from the `Selection`. 23% of the mailbox reaches the model
   through the HTML path; whether it classifies as well as plain text is a
   question this phase can answer for free and cannot answer later.

### Manifest

Every results file opens with: `model`, `prompt_id`, `body_chars`,
**`prompt_hash`**, **`extraction_version`** **[amended — A2]**, letter order,
sample-plan hash, git commit, timestamp. The `prompt_hash` (over
`build_system_prompt()`'s output) catches an edit to `categories.DESCRIPTIONS`
made without bumping the id — two incomparable runs filed under one label
invalidates every conclusion after it. `extraction_version` does the same job
for a silent change to body selection.

---

## 4. The iteration loop

- `eval/results/<run_id>.jsonl` — manifest + one prediction row per message.
  No content, so **committed**: the regression record, re-scorable forever.
- `eval/RESULTS.md` — one row per run (id, knobs, accuracy + CI, both
  `To Action` numbers, calibration gap, retained p10) plus a one-line *what
  changed and why*.
- **Paired comparison, not two accuracy numbers.** Same messages, so report
  flip counts (`n_fixed` / `n_broken`, McNemar). `+2 points from 4 fixed / 0
  broken` is a real change; `+2 from 12 fixed / 8 broken` is noise.
- **Lock-box protocol.** `predict` runs all 200; `score` reports dev-140
  unless `--holdout` is passed. Pick the winner on dev, *then* open the box
  once on that run — scoring every historical run on holdout and taking the
  best just makes it a second dev set. `--holdout` warns and appends to
  `RESULTS.md`, so the number of openings is on the record. At n=60 the
  interval is ~±12 points: the holdout's job is to detect inflation
  (dev 90 / holdout 72 is a finding), not to give a precise number.
- **Experiment order** — cheapest and largest expected effect first:
  `body_chars ∈ {0, 300, 800, 1500, 3000}` × `model ∈ {llama3.1:8b,
  llama3.2:3b}`, ten prediction runs, all scored offline afterwards.
  `body_chars=0` is a real baseline: if sender+subject is within noise of
  1500, the backfill gets 4× cheaper. Prompt variants then run on the best two
  configs.

---

## 5. Prompt candidates

A registry `PROMPTS: dict[str, Callable[[tuple[Category, ...]], str]]` in
`app/classifier.py` — `DESIGN.md` already names it as the home of prompt
templates. `PROMPT_VERSION: int` becomes `prompt_id: str`, and
`logbook.Record.prompt_version` changes `int | None` → `str | None` (cheap
now, with no real log rows written; annoying after a backfill).

| Id | Change | Hypothesis | Instrument |
|---|---|---|---|
| `v1` | current, as committed | baseline | — |
| `v2-ordered` | precedence as a numbered total order (Personal → To Action → Bookings → Receipts → Promotions → Updates), "choose the first that applies" | removes the "To Action beats everything" / Personal contradiction | accuracy, Personal↔To Action cell |
| `v3-letters` | precedence stated in letters (`F`, then `A`, then `C` over `B`…) | category *names* late in the prompt raise those word-tokens, competing with the letter tokens | **`retained_mass`** — the field this hypothesis was built for |
| `v3b-both` | `F (Personal)` form | keeps readability if `v3` costs accuracy | accuracy + retained_mass |
| `v4-format` | one line describing the input shape (`From:`, `Subject:`, blank line, body) | nothing currently tells the model what it is reading | accuracy |
| `v5-truncation` | "the body may be truncated mid-sentence; classify from what is shown" | already an open question in memory; weak prior — it can't recover unseen text, only stop an abrupt ending reading as a different genre | `To Action` recall at low `body_chars` |
| `v7-asymmetric` | "when genuinely torn between To Action and anything else, choose To Action" | a prompt-level thumb on the scale beside the floor | `to_action_retention` **and** the calibration gap, since it will distort the distribution |

---

## 6. Tests vs. what the eval set measures

**Unit-tested** — all new Phase 2 code, no Ollama, no Gmail:

- Weighting: disjointness enforcement, `w_h` derivation, weighted accuracy
  against a hand-computed example, and the failure raised when strata overlap
- Metrics: confusion matrix, both recall variants, calibration bucket
  boundaries (pin whether `0.8` lands in `[0.7,0.8)` or `[0.8,0.9)`), Wilson
  interval on a known case, TV distance, permutation stability count
- `score()` end to end over a synthetic results file — no mocks
- Resume: done ids skipped, a half-written trailing line tolerated on read,
  one flushed append per decision (same strict-write / tolerant-read split as
  `app/logbook.py`)
- Schema round-trip for both JSONL files; unknown category rejected on write
- **Content-leak guard**: key allowlist over `labeled.jsonl` / `sample.jsonl`
- `prompt_hash` guard: editing `categories.DESCRIPTIONS` changes the hash
- **Done [A1]:** `gmail_client.py` contains no write call (source-level,
  mirroring the `decision.py` purity test); MIME tree shapes, `rfc822`
  ordering, retry and backoff; `message_body` stripping, stub boundary cases
  and `parsed_ok`

**Measured by the eval set, never asserted in a test:** accuracy, `To Action`
recall and retention, calibration gap, permutation stability, latency on real
bodies, which prompt wins, which model wins, what `body_chars` should be,
whether the HTML path classifies as well as the plain path, and whether a
`retained_mass` floor is justified at all.

---

## Build order

1. ~~`app/gmail_client.py` (read half) + tests, incl. the no-write assertion~~
   **Done — `03110e6`.** Split into `gmail_client` + `message_body` per A1;
   222 tests, ruff clean, no new dependencies.
2. **← next.** `label_eval.py sample` → `eval/sample.jsonl`,
   `eval/strata.json` (committed)
3. `label_eval.py label` + cache → ~1.5–2 hours of hand-labelling
4. `prompt_id` migration in `classifier.py` and `logbook.py` + the `PROMPTS`
   registry
5. `eval/run_eval.py` — predict/score split, all metrics, `eval/results/`
6. Baseline run → `body_chars` × model sweep → prompt sweep
7. `label_eval.py recheck` — the self-consistency ceiling
8. Open the lock-box **once**; write measured thresholds and the model
   decision back into `DESIGN.md`, findings into `docs/PLAN.md`

Per `CLAUDE.md`, each of these is planned before it is written, and nothing is
committed by Claude — changes are left in the working tree and reported.

---

## Verification

**Per step, before moving on:**

- `uv run pytest` — the existing tests plus the new ones, green
- `uv run ruff check .` — clean (`scripts/label_eval.py` is *not* exempt;
  only `scripts/spike_*.py` are, per `pyproject.toml`)
- `git status` — `eval/labeled.jsonl` and `eval/sample.jsonl` appear;
  `data/eval_cache/` and `data/eval_frame.jsonl` do **not**

**Read-only scope, verified not assumed:** the no-write source test passes,
`app/auth.py:SCOPES` still reads `gmail.readonly` alone, and the token is
unchanged after a full labelling session.

**Sampling:** `label_eval.py sample --seed 7` twice produces byte-identical
`sample.jsonl`. Stratum sizes in `strata.json` sum to the frame count returned
by `gmail_client.count(frame_query)`, and every message id appears in exactly
one stratum.

**Labelling:** run `label` over 10 messages, `Ctrl-C`, re-run — it resumes at
11 with no duplicates and no lost rows. `label_eval.py verify` reports every
labelled id present in both the sample plan and the cache.

**Harness:** `score` on a hand-built synthetic results file with known
predictions reproduces accuracy, recall and the confusion matrix computed by
hand. Then `predict` on a 10-message slice, and `score` the same file twice at
different `--threshold` values — the second returns instantly, hits neither
Ollama nor Gmail, and moves the `Needs Review` rate in the expected direction.

**Gate (from `docs/PLAN.md`), all answerable from the output:** overall
accuracy tolerable; `To Action` recall *and* retention high; calibration gap
meaningfully positive; permutation stability high; and measured values for
`confidence_threshold` / `to_action_floor` that satisfy `T + F < 1`, confirmed
by `Config` accepting them.

---

## Open question carried into this phase

**Refresh-token lifetime in Testing status** (from Phase 0). Assumed 7 days;
the current consent dates from 2026-09-08 13:41 UTC, so the assumption
predicts expiry around 2026-09-15. Labelling is a 1.5–2 hour sitting, so this
only bites if it starts late in the window. When auth does break, record the
actual interval in `docs/PLAN.md` — it resolves the last Phase 0 spike
question for free.
