# Phase 2 — Eval harness

Companion to `docs/PLAN.md`, which says what Phase 2 contains and what has to
be true before Phase 3. This document says *how* it gets built.

**Start at [Build order](#build-order).** It says which step is current and
what each finished one produced. Then read the amendment log below, which
lists every place the build knowingly departed from the agreed design.

How to read the rest: **§1-§6 are the plan as originally agreed**, amended in
place where the build diverged. The **`Step N implementation` sections near
the end are the current, detailed plans** for individual steps, written just
before each is built. Where the two disagree, the step section is newer.

> **Provenance.** Agreed in a planning session on 2026-09-09 that was lost
> before it could be written down. Recovered on 2026-09-10 from the Claude
> Code transcript on disk (`~/.claude/projects/<slug>/*.jsonl`, the
> `ExitPlanMode` record of session `1ee31ef6`). The body below is that plan,
> amended where the build has since diverged from it. Amendments are listed
> immediately below and marked **[amended]** where they appear. A1-A3 date
> from 2026-09-10; A4 from 2026-09-11; A5-A6 from 2026-09-12; A7-A8 from
> 2026-09-12 and 2026-09-13.

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
A1: `EXTRACTION_VERSION` (`app/message_body.py`, currently `"v4"` — see A7) is now
one of the inputs that changes predictions, so two runs either side of a bump
are not comparable. It is recorded alongside `model`, `prompt_id` and
`body_chars`, and it belongs in the list of things that force re-inference.
§3 (Manifest) and §4 amended.

**A3 — `classify()` requires both text parts.** `text_html` briefly carried a
`""` default. Removed, so no caller can reach the model having supplied only
`text_plain` and silently classify HTML-only mail — 23% of the mailbox — on an
empty body. The default made a structural guarantee conventional. §5 unaffected;
noted here because it changes the signature the harness calls.

**A4 — The frame is pinned to absolute dates.** `newer_than:1y` is evaluated
relative to the moment the query runs, so the frame moved every day: mail
arrived at the front, a day aged off the back, and `--seed 7` drew from a
different population on each run. The "byte-identical twice" criterion passed
only because the CLI reused the cached frame listing — and that listing lives
in `data/`, which is gitignored, so `eval/sample.jsonl` would have been
committed and auditable while the population it came from existed on one
laptop. Replaced with `FrameSpec`, which pins `after:`/`before:` at sample
time and records the window in `strata.json`. Widening becomes "the band
immediately older than the frame", which is disjoint from it by construction
rather than by filtering afterwards — that also removes the
`frame_query.replace("newer_than:1y", ...)` string surgery, which silently
no-opped on a custom window. The `--frame` flag is replaced by `--end` and
`--window-years`. Deletion is still not covered — a deleted message leaves
the frame whatever the query says — and remains handled downstream by
`data/eval_cache/`. Found in review, not planned.

**A5 — `u` records a flag rather than deferring.** The Step 3 section defined
`u` as "defer, re-presented after the main pass", with `unsure` set when the
message came back round. That made `u` mechanically identical to `s` until the
end of a sitting, and left the flag living in session state: quitting with
deferrals outstanding lost it, and persisting it meant either a label-less row
in `labeled.jsonl` — breaking both the key allowlist and the reject-unknown-
category rule — or a second file in `data/`. As built, `u` toggles "unsure" for
the message on screen and the following digit writes the row with
`unsure=True`, so the judgement and the flag are recorded in the same instant.
The deferred queue disappears and `pending()` takes the sitting's skipped ids
in its place. `s` is unchanged: no row, reported loudly by `verify`. The flag
itself is kept because "the human was not sure either" is a different finding
from "the model is wrong", and those rows are the ones expected to flip in the
step 7 recheck. Raised by the human in review.

**A6 — labelling UI surface.** Digit keys derive from `tuple(Category)` —
`1 To Action  2 Receipts  3 Bookings  4 Updates  5 Promotions  6 Personal` —
so there is no second ordering list to drift from the enum; the mock below had
Receipts and Bookings the other way round. `--stratum`, carried over from §2,
is dropped: `label --stratum S_action` tells the labeller that everything they
are about to see was mined for action keywords, which is the same leak the
Step 3 section removed from the screen. `--limit` stays.

**A7 — Extraction `v3`, found while labelling.** A newsletter reached the
labelling UI with no content in it: the plain part said only "this email is
only available in HTML", and the stub rule missed it *twice over* — 707
characters (361 of them one tracking URL) against a ratio of 1.5 (deflated by
104 zero-width spacers in the HTML). Neither half of the rule could have been
retuned into catching it, because both were measuring padding. `v3` therefore
measures both tests over **informative** characters (URLs and zero-width
discounted), lets an explicit "only available in HTML" declaration waive the
*length* test while still facing the ratio guard, and rewrites URLs in the
selected body down to their host — a median 28% of a body, over 90% of the
budget at the extreme, and worst-case tokenisation on a CPU-only box.
**`v4`, an hour later**, strips a `text/plain` part that is really an HTML
document (1 of 64 cached messages — 120,567 characters of markup the old rule
preferred over the real body) and adds U+034F to the spacer set, which
outnumbers the character `v3` added by five to one in this mailbox. Bumped
rather than folded into the unused `v3` because one version label meaning two
rules is the failure the field exists to prevent. Four source flips across the
cache, each inspected by hand; the clearest is a Skyscanner mailout whose plain
part is 99.2% tracking URL, leaving 52 characters of words.

`STUB_MAX_CHARS` is deliberately **unchanged**: whether 200 is the right number
needs a distribution, and the eval cache is the instrument once it is full.
Verified over the 22 messages cached so far — two source flips, both inspected
by hand, both improvements. Built now rather than deferred at the human's call:
no eval run exists, so the bump costs nothing, and a known bug left in the tree
is a bug that gets forgotten. §1 amended.

**A9 — The recheck draws 30, and the draw is pinned to a file.** §2 sketched
`recheck --n 20 --seed 99`, recomputed from flags each time. Two changes. **30
rather than 20**, at the human's call, for variety: at n=20 the Wilson interval
on the ceiling is about ±0.15, and a uniform draw that size stands a good
chance of containing no `Bookings` at all. **Pinned to `eval/recheck.json`**
(ids and provenance, committed, `RECHECK_KEYS` checked on write) rather than
re-derived, which is A4's argument one level down: a draw computed from CLI
defaults moves silently the day someone types a different `--n`, and
`verify --pass 2` would then check completeness against a set nobody labelled.
The draw only ever *grows* - a larger `n` at the same seed extends it in place,
which is safe because `recheck_draw` shuffles then takes, so `n=40` is `n=30`
plus ten. A different seed, or a smaller `n`, is refused. §2 amended.

**A8 — The harness is `app/` code, not `eval/` code.** §3 put predict/score in
`eval/run_eval.py`. That cannot be tested: `pyproject.toml` packages only
`app`, `tests/conftest.py` adds no path shim, and §6 asks specifically for
`score()` end to end with no mocks plus tests for weighting, Wilson intervals,
TV distance and calibration boundaries. Step 2 hit the identical problem and
its own module-boundary block already says `eval/  data only`, which §3
contradicted. As built: `app/evalscore.py` (pure), `app/evalrun.py` (Ollama and
disk), `scripts/run_eval.py` (CLI). `eval/` holds `sample.jsonl`,
`labeled.jsonl`, `strata.json`, `results/` and `RESULTS.md`, and no code.

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
| Sampling frame | one year of received mail, ~6,300 (pinned to absolute dates — see A4) |
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
BodySource = Literal["plain", "plain_markup", "html", "stub_fallback", "none"]

@dataclass(frozen=True)
class Selection:
    text: str; source: BodySource; parsed_ok: bool

EXTRACTION_VERSION = "v4"          # v2 as agreed; see A7
STUB_MAX_CHARS = 200
STUB_HTML_RATIO = 2.0
STUB_MARKERS = (...)               # A7
ZERO_WIDTH = {...}                 # A7

def strip_html(html: str) -> tuple[str, bool]: ...
def select_body(text_plain: str, text_html: str) -> Selection: ...
def shorten_urls(text: str) -> str: ...          # A7
def informative_length(text: str) -> int: ...    # A7
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
| `v8-updates` | `Updates` described as "informational; no action ever needed", dropping **"low-priority"** | the description welds a structural claim to a judgement about the reader's interest, and only the first is the taxonomy's business. A serious, high-interest, no-action email - the MEAA/CGA statement to actors - reads as excluded by its own category | accuracy on the `Updates` row, and the `Updates`/`Personal` cell in particular: the hypothesis is that "low-priority" pushes substantive bulk mail toward `Personal`, which *keeps `INBOX`* and is therefore not a free error |

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
2. ~~`label_eval.py sample` → `eval/sample.jsonl`, `eval/strata.json`~~
   **Done.** `app/evalset.py` + thin CLI; the frame pinned to absolute dates
   per A4; 258 tests, ruff clean, no new dependencies. The sample itself is
   not yet drawn against the real mailbox.
3. ~~`label_eval.py label` + cache → ~1.5–2 hours of hand-labelling~~
   **Done.** `app/evallabel.py` + the keypress loop and `verify`; `--relabel`
   added when `b` proved to be sitting-local. **200/200 labelled**, `verify`
   clean, `n_h == sampled` in every stratum. 318 tests, ruff clean, no new
   dependencies. Ground truth after a full review pass (24 corrections via
   `--relabel`): `Promotions` 82, `Receipts` 42, `To Action` 33, `Updates` 21,
   `Personal` 16, `Bookings` 6; 6 flagged `unsure`. Labelling found two
   extraction bugs invisible to the test suite (A7, audited at n=200 in
   `docs/BACKLOG.md`) and produced the labelling rules recorded below.
4. ~~`prompt_id` migration in `classifier.py` and `logbook.py` + the `PROMPTS`
   registry~~ **Done.** `PROMPT_VERSION: int` → `DEFAULT_PROMPT_ID: str` plus
   `PROMPTS`, `system_prompt()` and `prompt_hash()`; `logbook.Record`
   field renamed. Registry holds `v1` only; variants land with the run that
   tests them.
5. ~~`eval/run_eval.py` — predict/score split, all metrics, `eval/results/`~~
   **Done** as `app/evalscore.py` + `app/evalrun.py` + `scripts/run_eval.py`
   per A8. 372 tests, ruff clean, no new dependencies. Verified end to end
   against real Ollama on a 10-message slice.
6. ~~Baseline run → `body_chars` × model sweep → prompt sweep~~ **Done.**
   16 runs, ~3,400 calls, zero failures — see **Step 6 results** below.
   Selected: `llama3.1:8b`, `body_chars=300`, prompt `v9b-bookings`, `T=0.8`,
   and `Bookings` added to `KEEPS_INBOX`. Dev action accuracy 0.900,
   `To Action` retention 24/24.
7. **← current.** `label_eval.py recheck` — the self-consistency ceiling.
   **Harness built**, 2026-09-13: `recheck` + `--report`, the pinned draw per
   A9, `verify --pass 2` checking against that draw, and the keypress loop
   shared with `label` rather than copied. 393 tests, ruff clean, no new
   dependencies. **Done 2026-09-14**: ceiling 28/30 six-way, **30/30 on
   keeps-INBOX**; both flips were one unevenly-applied precedence rule, which a
   nine-row `S_human` sweep then corrected in seven places; all fourteen runs
   re-scored and `eval/RESULTS.md` rewritten. The selected configuration
   survived — see "Step 7 results" below.
8. ~~Open the lock-box **once**; write measured thresholds and the model
   decision back into `DESIGN.md`, findings into `docs/PLAN.md`~~ **Done
   2026-09-14. Gate PASSED.** Holdout 0.800 [0.68, 0.88] six-way, action
   accuracy 0.900 — identical to dev — 1 costly error, retention 9/9. No
   detectable inflation. `DESIGN.md` and `docs/PLAN.md` written; the measured
   configuration now ships in code (`body_chars=300`,
   `DEFAULT_PROMPT_ID="v9b-bookings"`).

**Phase 2 is complete.** Phase 3 is the writer end — and its first hard rule
stands: no write path ships before `scripts/undo_run.py` exists and has been
run successfully against real messages.

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

## Step 2 implementation — `label_eval.py sample`

Agreed 2026-09-10. Resolves six things §2 left open; the resolutions amend §2
where they conflict with it.

**Scope.** Produces `eval/sample.jsonl` and `eval/strata.json` (committed) plus
`data/eval_frame.jsonl` (gitignored). No labelling, no fetching, no Ollama —
and **zero `messages.get` calls**, which is what dropping `internal_date`
bought.

### Module boundaries

```
app/evalset.py          pure: strata, assignment, drawing, splits, weights
scripts/label_eval.py   CLI only: argparse, progress output
app/gmail_client.py     one addition, below
eval/                   data only
```

Logic lives in `app/` rather than `scripts/` because `scripts/` is not
importable — `pyproject.toml` has `packages = ["app"]`, `tests/conftest.py`
adds no path shim, and `measure_bodies.py` copies `spike_extract` rather than
importing it for exactly this reason. §6 asks for unit tests on disjointness,
`w_h` derivation and the content-leak guard, none of which can be written
against a script. It is also not throwaway: `docs/PLAN.md` Phase 5 has
`app/metrics.py` aggregating "the log **and the eval results**".

**`gmail_client` addition.** `search_ids` discards `threadId`, which `S_human`
needs. Inverted rather than duplicated:

```python
def search_refs(svc, query, limit=None) -> list[tuple[str, str]]:  # (id, thread_id)
def search_ids(svc, query, limit=None) -> list[str]:               # now a wrapper
```

`messages.list` returns both fields already, so this costs nothing at the API
and keeps one pagination loop.

### Sampling algorithm

0. **Pin the window** — `FrameSpec.ending()` resolves `--end` (default today,
   local date) and `--window-years` into `after:YYYY/MM/DD before:YYYY/MM/DD`,
   recorded in `strata.json`. See A4: a relative window makes the sample
   irreproducible the next day.
1. **Enumerate the frame once** into `data/eval_frame.jsonl` as
   `{message_id, thread_id}`, ~6,300 rows, ~13 list calls, with the frame
   query as a header line so a cache built for another window is not reused.
   Reused unless `--refresh`, so re-sampling never re-hits the API.
2. **Resolve mined membership** in full (not sampled — `N_h` must be exact).
   `S_action` and `S_txn` are the frame query AND a subject clause. `S_human`
   is `is:starred` within the frame, plus the `thread_id`s of `from:me`
   intersected against the frame's — sent mail is not in the frame, so the
   thread is the bridge, and it needs no `get`.
3. **Partition the frame** by ordered assignment `S_action → S_human → S_txn →
   Residual`, first match wins. This is the partition the weights need and is
   independent of what gets sampled.
4. **Draw `R` first** — 100 uniform over the whole frame. The
   representativeness anchor, and the reason `S_other` is gone.
5. **Top up each mined stratum** to target from `(stratum ∩ frame) − taken`.
6. **Extension on shortfall** — re-query the band immediately older than the
   frame, reaching back 2, 3 then 4 years. Disjoint from the frame by
   construction. Marked `draw="extension"` and excluded from the weighted
   estimate. Any residual shortfall goes to `R`, drawn **from inside the
   frame**: `R` never extends, or the mailbox estimate stops describing the
   mailbox.
7. **Split 70/30 dev/holdout within each stratum**, drawn from a derived seed
   so `--seed 7` reproduces it. Assigned here, applied at score time.

**Budget change:** `S_other` is dropped as redundant with `R`, and its 20
labels move to `R` rather than to the mined strata — `R` 100, `S_action` 35,
`S_human` 35, `S_txn` 30, total 200. `S_other`'s purpose was "promo bulk",
which `R` already delivers in proportion, so the 20 buy a tighter mailbox
estimate instead of more of the commonest class.

**Why `n_h` stays valid:** a uniform draw from a stratum, followed by a further
uniform draw from that stratum minus the first, is a simple random sample of
the stratum at the combined size. So `n_h` = R's members landing in `h` plus
the top-up, and `w_h = N_h/n_h` holds.

**Determinism:** ids are `sorted()` immediately before every draw. Python
randomises string hashing per process, so `set` iteration order differs
between runs and `random.sample` picks by position — seeding alone does not
give a reproducible sample. This is the mechanism behind the "byte-identical
twice" criterion.

### Schema amendments to §2

`eval/sample.jsonl` — `internal_date` **dropped** (`messages.list` does not
return it; it is free at label time from the cache, and nothing reads it at
sample time). `draw` **added**, because §2 conflated two things:

```json
{"message_id":"18f2...","stratum":"S_action","draw":"mined","split":"dev","seed":7}
```

`stratum` is the partition cell, used for weights. `draw` is provenance —
`R` / `mined` / `extension` — so the honest `To Action` number in §3 is the
one filtered to `draw == "R"`, keyword-mined examples being the ones that say
"overdue" on the tin.

`eval/strata.json` records `frame_query`, `frame_size`, `seed`, and per
stratum `name`, `query`, `window_years`, `N_h`, `n_h`, `n_extension`.
Extension rows are counted in `n_extension` only, keeping `w_h = N_h/n_h`
defined over the frame.

### Tests — pure, no Gmail

Partition is disjoint and `Σ N_h == frame_size`; first-match-wins ordering;
`R` from the whole frame and top-ups from the residual with nothing sampled
twice; byte-identical output for a repeated seed, including a fake frame whose
set order differs; shortfall → extension → flagged and excluded → residual
shortfall tops up `R` inside the frame; `R` never extends; split is 70/30
within each stratum and reproducible; `S_human` thread intersection both ways;
key-allowlist guard on both committed files; frame cache reused, `--refresh`
re-queries.

### Verification

`--seed 7` twice is byte-identical; `Σ N_h == count(frame_query)`; every id in
exactly one stratum; `git status` shows the two `eval/` files and not
`data/eval_frame.jsonl`.

---

## Step 3 implementation — `label_eval.py label`

Agreed 2026-09-11. The long pole of the phase: steps 5 onward are blocked on
the labels existing.

**Scope.** `label` and `verify` subcommands, the message cache, and 200
hand-labelled messages. Produces `eval/labeled.jsonl` (committed) and
`data/eval_cache/` (gitignored).

### Module boundaries

```
app/evallabel.py        pure: records, corrections, progress, the cache, rendering
scripts/label_eval.py   the keypress loop and terminal I/O
```

A new module rather than growing `evalset.py` (already ~560 lines), on the
same line as before: anything testable without a terminal lives in `app/`.

### Decisions taken

| Decision | Choice |
|---|---|
| Presentation order | Randomised across strata, `Random(seed + 2)`, deterministic so resume keeps the order |
| Stratum in the UI | **Never shown.** `S_action` on screen is a direct hint at `To Action` |
| `b` (back) | Appends a correction; last row in file order wins, **within a pass**. Reaches back through the current sitting only - `--relabel <id>` covers earlier ones |
| Fetch | Batches of 50, cached at fetch time; `--batch` flag to lower it |
| `unsure` | `u` flags the message on screen; the next digit writes `unsure=True` **[A5]** |
| Blind recheck | Pass 2, only after pass 1 is complete. `verify` refuses otherwise |
| Unclassifiable mail | Not skipped - it is a taxonomy signal. See below |

**Why batches of 50 and no throttle.** `docs/BACKLOG.md` measured a rate limit
between 150 and 175 consecutive `messages.get`. Fifty is ~25 seconds and well
under it, and the ~25 minutes of human labelling between batches resets the
window entirely. Caching at fetch rather than label time means quitting after
ten leaves the other forty on disk, so resuming costs nothing.

**Corrections never cross passes.** Step 7's recheck writes `pass=2` rows, and
those are a separate measurement of self-consistency, not corrections to
pass 1. Resolution is last-row-wins *within* a pass. File order, not
timestamp - two appends in the same second would tie.

**An unclassifiable email is a finding, not a skip.** `docs/PLAN.md` already
lists the `Personal` category and the taxonomy as Phase 2 checkpoints where
reality is expected to argue back. `s` remains in the UI as an escape hatch
and `verify` reports any unlabelled id loudly. Adding a category touches
`categories.py` (member, description, letter mapping), the prompt,
`KEEPS_INBOX`, and a new `Agent/` label - but **step 3 is the cheapest moment
for it**, because no eval run has happened yet, so there is no `prompt_hash`
to invalidate and no historical results to re-score.

### Signatures

```python
@dataclass(frozen=True)
class LabelRecord:
    message_id: str; label: Category; unsure: bool; labelled_at: str; pass_no: int

@dataclass(frozen=True)
class Cached:
    message_id: str; sender: str; subject: str
    internal_date: str; text_plain: str; text_html: str

def ordering(sample: Sequence[Sampled], seed: int) -> list[str]
def resolve(records: Sequence[LabelRecord], pass_no: int = 1) -> dict[str, LabelRecord]
def pending(order, resolved, skipped=()) -> list[str]
def render(message: Cached, body_chars: int, show_full: bool, *, now, position, total) -> str
def append_record(record: LabelRecord, path: Path) -> None      # flushed per write
def load_records(path: Path) -> tuple[list[LabelRecord], int]   # tolerant read
def cache_put(message: Message, directory: Path) -> None
def cache_get(message_id: str, directory: Path) -> Cached | None
def cache_missing(ids: Iterable[str], directory: Path) -> list[str]
def unfinished(records, sample_ids, pass_no=1) -> list[str]     # the pass-2 guard
def counts_by_stratum(sample, resolved) -> dict[str, dict[str, int]]
```

Five refinements to that list, made while building and none of them design
changes. `label` is typed `Category` rather than `str`, which is how "unknown
category rejected on write" becomes unrepresentable rather than a check that
could be forgotten. `load_records` returns a count of unreadable lines beside
the rows, mirroring `logbook.read_all` exactly, so `verify` can report damage
instead of silently reading past it. `pending` takes the sitting's skipped ids
where it used to take deferrals **[A5]**. `cache_missing` is what lets the
fetch loop ask for a chunk without re-reading every file. `unfinished` and
`counts_by_stratum` are the two things `verify` reports that nothing else
computes — the second being the labelled-not-sampled `n_h` that step 5 weights
by.

Paths are parameters here with no defaults, and `scripts/label_eval.py` supplies
them. The pure module cannot then write into the real `data/` because a test
forgot to redirect it.

### The UI

```
[47/200]                                          2026-03-11  (6 months ago)
From:     no-reply@booking.com
Subject:  Your reservation is confirmed

Thank you for booking. Your stay at ... [1,500 of 4,207 chars — m for more]

1 To Action  2 Receipts  3 Bookings  4 Updates  5 Promotions  6 Personal
u unsure (flag)    s skip   b back   m more   q save and quit
```

Body shown is `message_body.select_body()` output - what the model will
actually read. **The truncation marker is informational only**: ground truth
is what the email *is*, not what the model can see, or every `body_chars`
change would silently redefine the target. Stated in the CLI help alongside
the arrived-at rule.

`--relabel <id> ...` re-presents already-labelled messages instead of
continuing the queue. Needed because `b` walks back only through the current
sitting, while `pending()` filters out every resolved id - so without it a
decision made on a previous day is unreachable, and a rule refined at message
150 cannot be applied to something labelled at message 20. It appends a
correction like `b` does, and leaves the pending queue untouched.

Single keypress via stdlib `termios`/`tty` - 200 messages is 200 spurious
Enters otherwise - falling back to line mode when stdin is not a tty, so tests
need no pty.

### The labelling rules — how a hard case gets decided

Derived while labelling, each from a real message. Written down because step 7
measures self-consistency: a rule applied differently on message 20 and message
180 shows up as labeller noise and eats into the ceiling the model is scored
against. A rule that is *wrong* but consistent is far cheaper - it appears as a
clean confusion-matrix cell that can be seen and argued with.

**Before any of them: never label to what the model is expected to manage.**
A paid invoice is `Receipts`, even if the model will probably read "invoice"
and say `To Action`. Labelling it `To Action` to match that expectation bakes
the model's weakness into the target, and three things break at once: whether
it can tell becomes unmeasurable, because the target agrees with the error; a
better model that *does* understand "paid" scores worse; and every comparison
in step 6 is anchored to the capability assumed at labelling time. This is the
truncation-marker rule one level up - ground truth is what the email *is*, not
what the model can see, and equally not what it can understand. Labelled
correctly, the question becomes a `Receipts` -> `To Action` cell in the
confusion matrix and a prompt fix that can be scored.

There is a sampling reason too. `S_action` is mined on
`subject:(invoice OR "payment due" OR overdue ...)`. If ground truth follows
those same keywords, the eval measures whether the model can spot the words
used to *select* the messages rather than whether it understands them. The
strata are defined by observable queries and never by a guess at the label, to
stop the label leaking into the frame; labelling by keyword leaks it in from
the other end.

A note on `Updates` while labelling: its description reads "low-priority
informational content, no action ever needed". Only the second half is a rule.
A serious, high-interest, no-action email is still `Updates` - what the label
decides is whether the message stays in the inbox, not whether it is worth
reading, and `Agent/Updates` is a shelf rather than a bin. The wording is a
prompt candidate in §5 (`v8-updates`) because the model is given it too.

Alongside the arrived-at rule (label what it should have been **when it
arrived**), four questions, in order:

**1. Does the email demand something of you, or inform you?** Informing is
`Updates`, even when the subject matter might prompt work of your own. A Sentry
weekly error digest for a hobby app informs; nobody is waiting on a reply, and
next week's digest supersedes it. The trap is reading `To Action` as "concerns
something I might work on", which makes every newsletter on a topic you care
about actionable and empties the category of meaning.

**2. If it demands, does ignoring it cost *you* anything?** A bill, a renewal,
an expiring verification - ignoring those costs you, so `To Action`. A review
request, a survey, an NPS prompt - ignoring those costs you nothing and the
sender wants your time, so `Promotions`. Solicitation is not obligation.

The case to slow down for is a matter of your own that is **blocked on your
reply**: "confirm your details so we can continue" stalls something you
started, so it is `To Action`, while "how did we do?" from the same support
thread in the same week is a survey. Arriving inside a thread you opened is not
what decides it, and neither is a human-looking sender - a CSAT survey fires on
ticket closure and is automated mail, so it is not `Personal` either, which
matters because `Personal` keeps the inbox.

Noted against the gate: this is the first place rule 2 and a category
*description* disagree - "marketing content trying to sell something" describes
a Hostelworld review request better than a Google support survey, and `Updates`
reads more naturally for the latter. Taken as `Promotions` for consistency,
since the underlying act is identical and the error is free. If several more
land this way, the taxonomy is the thing to revisit, not the labels.

**3. A deadline only counts when the thing expiring is already yours.** Your
subscription lapsing or your domain needing renewal is a real deadline:
`To Action`. A free trial you never asked for, or 50% off ending Sunday, is a
marketing urgency device: `Promotions`. Note the near neighbour from the same
sender - "your trial ends in 3 days and your card will be charged" *is*
`To Action`, because something of yours expires and money moves.

**Boilerplate is not a demand.** Delivery mail is the test case, being both
high-volume and time-sensitive. "Out for delivery" informs, so `Updates`; "be
home Tuesday 9-11 or it returns to the depot" is a specific act at a specific
time on something already yours, so `To Action`. But most carriers append "a
signature may be required" to *every* notification, and counting that would
drag the whole class into `To Action` and flood the inbox. The test is a named
window, a reschedule link or an address confirmation - not a footer mentioning
presence. (`Updates` vs `Bookings` for a parcel is a free error; a parcel in
transit is not a reservation, but both archive.)

This class also tests the arrived-at rule harder than any other. A delivery
notification is inert within hours, and by labelling time the parcel arrived
weeks ago. It was `To Action` **when it arrived**. Letting hindsight decide
collapses every time-sensitive message into `Updates` and measures `To Action`
recall against a target that quietly excludes the most urgent mail in the
mailbox.

**Money: tense is the discriminator.** Periodic statements - investing,
banking, betting - are `Receipts`. The operative half of that category's
description is "record-keeping only", not "a transaction", and the functional
argument is stronger still: a label is only worth anything if the set it
produces is the set you would go looking in. Statements scattered into
`Updates` make `Receipts` incomplete for its single job.

The rule is *not* "anything to do with money", which reads on both sides of the
expensive boundary. Money that has already moved - receipts, statements,
payment confirmations - is `Receipts`. Money being demanded of you - invoices,
bills, failed payments, a card about to be declined - is `To Action`.

**Money scheduled to move automatically is also `To Action`**: "your membership
will auto-renew on November 28" is a pre-billing notice, and the only moment at
which the charge can be stopped. It is strictly worse than an ordinary bill,
because the default outcome is payment rather than a reminder. Note this is the
*opposite* tense from the renewal receipts filed under `Receipts` - the
discriminator is before-or-after the charge, not how large it is. A change you
cannot prevent and that costs nothing (Cursor's "we're auto-upgrading to
Composer 2") stays `Updates`. Missed in the first draft of this rule and caught
by the human while relabelling. A bill
filed as `Receipts` is a missed bill, which is the only error in this system
that costs anything. Where a statement carries an actual demand ("confirm your
risk profile"), rule 2 takes over and the statement wrapper does not matter.

**`Personal` is mode of address, not importance.** A joint MEAA/CGA statement
to actors about misconduct allegations is sincere, human-written, serious and
directly relevant - and it is `Updates`, because it opens "Dear Actors" and
goes to a whole membership. The definition is "written by a real person
directly to the reader, not automated or bulk mail", and the test is whether
someone wrote *to you*, not how much the content matters.

Load-bearing, because `Personal` is one of the two categories in
`KEEPS_INBOX`. If weighty bulk mail qualifies, the inbox fills with every
important-sounding announcement and the category stops meaning "a human wrote
to me", which is the only thing that makes it worth keeping visible. Bulk
announcements from an organisation you belong to - union, guild, club, school,
body corporate - are `Updates` regardless of gravity.

`docs/PLAN.md` names `Personal` as a Phase 2 checkpoint where reality is
expected to argue back. The rule held here. Repeatedly *wanting* `Personal` for
serious bulk mail would be evidence the taxonomy is missing something, not
evidence of bad labelling.

**The order lifecycle, once, because it recurs constantly.** `S_txn` is mined
on `subject:(receipt OR order OR booking OR reservation OR confirmation)` and
contributes 34 of the 200, so consistency here shapes a whole stratum:

| message | label |
|---|---|
| "Order received / confirmed", with number and total | `Receipts` |
| "Shipped" / "out for delivery" / "delivered" | `Updates` |
| "Be home Tuesday 9-11 or it returns to the depot" | `To Action` |
| "Rate your purchase" | `Promotions` |

The discriminator between the first two is whether the email *is* the record or
merely reports on it. Hence the edge case: "we have received your enquiry"
carries no money and no reference worth keeping, so `Updates`.

**Precedence, for labelling: a human wrote it -> `Personal`**, even when the
message is also a booking or also a demand. A person writing "confirmed for
Tuesday 6pm, wear black" is `Personal`; the platform's automated confirmation
sitting in the same thread is `Bookings`. The question is who typed it, which
is a fact rather than a judgement and therefore survives 200 repetitions - and
it keeps `Personal` from eroding into "human mail with nothing asked of it".

The decisive argument is learnability, raised by the human while relabelling:
"did a person write this?" is detectable from the text - salutation, signature
block, reply chain, non-bulk phrasing - whereas "does this human email demand
something?" requires reading intent and has a genuinely fuzzy boundary. Two
real examples a sentence apart in tone, "it is important that your clearance is
completed" and "it may be best for yourself to raise this directly", would sit
on opposite sides of it. Split that way the model is penalised for failing a
distinction that changes nothing; kept together, `Personal` is a clean class
and `To Action` stays coherent as *automated* actionable mail.

Nothing is lost operationally: `Personal` is in `KEEPS_INBOX`, so a friend's
payment request still counts as saved under `to_action_retention`. Where the
model calls such a message `To Action` against a `Personal` target, the 6x6
matrix records a confusion while the collapsed 2x2 action matrix scores it
correct - both keep the inbox, and the metric with consequences absorbs it. `Personal`
vs `To Action` is therefore a **free error** on any human-written message; the
consequential mistake is `Bookings`, which archives it. The cost to note is
that `to_action_recall` becomes a measurement over *automated* actionable mail
- arguably the honest number, since human mail gets noticed regardless and the
agent's value is in the automated pile.

This is the contradiction §5's `v2-ordered` candidate exists to resolve in the
prompt. The labelling rule above and that candidate's ordering agree
deliberately, so ground truth and the winning prompt are not pulling in
opposite directions.

**Something you initiated that has not completed is `To Action`** - bounce
notices, failed sends, declined payments, stalled submissions. "Delay" and
"Failure" are the mail server's vocabulary, not yours: a real pair in the eval
set shows a Gmail *delay* notice carrying a hard `550` ("the account or domain
may not exist") followed two days later by the *failure* for the same
recipient. The delay notice was two days of runway to reach a travel supplier
another way; labelling it `Updates` on the strength of the word "delay" would
have thrown that away. Split on what happened to you, not on the term the
system used.

**The justification for a label has to be visible in the message.** Platform
activity notifications - streaks, "your post got 26 impressions", "14 others
reacted", year-in-review recaps - are `Updates`. They report a fact about what
you did; nothing is offered and nothing is sold. Calling them `Promotions`
because streak mechanics exist to drive retention appeals to how apps make
money, which is not in the email - and the model only sees the text, so the
target would be trained on evidence the input does not contain.

Contrast the casting call, whose textual evidence is a shoot date, a rate and
"submit for ONE role". Knowing the agency works for you helps explain a label;
it is not what carries it.

**[amended 2026-09-14, step 7]** That message was `To Action` when this rule
was written and is now `Personal`: the precedence rule above - a human wrote
it, so `Personal`, even when the message also demands something - takes it
first, and precedence outranks the demand test by construction. The point the
paragraph is making is unchanged, since the evidence is still in the text
either way. Recorded rather than quietly rewritten, because a worked example
that disagrees with `eval/labeled.jsonl` is exactly the drift this section
exists to prevent.

**4. A conditional demand is judged on the cost of being wrong, not on how
often it needs acting on.** "Changes were made to your Apple account - respond
if this wasn't you" needs nothing ninety-nine times in a hundred, which by
frequency alone reads as `Updates`. The hundredth is an account takeover with a
window measured in hours, and the expected cost is dominated entirely by it.
`To Action`.

This is not an exception to the rules above but the argument the system is
already built on: `to_action_floor` keeps `INBOX` when `p(To Action)` clears a
*low* bar even against a different argmax, precisely because the error is
asymmetric. Security notifications are therefore `To Action` as a class - new
device sign-in, password changed, recovery email added, 2FA disabled, "was this
you?" - which removes the case-by-case judgement that would otherwise drift
across 200 messages.

Phishing imitates this genre, and the taxonomy has nowhere sensible to put it.
It should not arise: `messages.list` defaults `includeSpamTrash` to false and
`gmail_client.search_refs` never overrides it, so anything Gmail already caught
is outside the frame by construction. If one does appear in the sample, that is
a taxonomy finding worth raising rather than a label to force.

The rules line up with the only error in this system that costs anything. A
missed bill is a real loss; a missed promotion is not a loss at all. Where a
rule is uncertain, the question to ask is which side of `KEEPS_INBOX` the
message belongs on - `Updates` vs `Promotions` is a free error, because both
archive, while either of those against `To Action` is not.

### Input fidelity — the cache is exactly what the model reads

Both paths call the same function with the same four arguments, so the eval
measures the pipeline that will actually run:

```
LIVE (Phase 3)                      EVAL (step 5)
gmail_client.fetch(svc, id)         cache_get(id)
  → sender, subject,                  → sender, subject,
    text_plain, text_html               text_plain, text_html
        └──────────► classifier.classify(...)
                       → build_user_message
                         → select_body(...).text[:body_chars]
```

Those four fields are necessary and sufficient; nothing else in `Message`
reaches the model. This is why A1 stores raw text parts rather than a finished
body - `select_body` runs fresh at eval time, so an `EXTRACTION_VERSION` bump
is re-scorable against the same cache - and why A3 removed the `text_html`
default.

`internal_date` is the one cached field the model never sees. It exists for
the arrived-at rule in the UI, and the cache docstring says so, so it cannot
drift into `predict` later.

### Two notes against step 5

**Prefilter hits never reach the model.** `app/prefilter.py` routes a
sender-domain allowlist match straight to `Agent/Promotions` with no LLM call,
and `decision.decide_prefilter_hit()` records `confidence=None` deliberately.
`docs/BACKLOG.md` estimates this covers roughly a third of the backlog, so
scoring the model's answer on those messages measures something the live
system would never do. Nothing extra needs caching - `sender` is already
there, so `run_eval` computes the hits at score time from `load_allowlist()`.
They get their own bucket in the report (a rule is right or wrong, with no
confidence) and are **excluded from the calibration table and the threshold
sweep**.

**Score prefilter hits on the action, not the 6-way label.** The rule can only
ever emit `Agent/Promotions` (`decision.decide_prefilter_hit`), so a
prefiltered message whose true label is `Updates` counts as an error under
6-way scoring while being entirely correct operationally - both archive. Found
while labelling a LinkedIn job digest from `jobs-listings@linkedin.com`, a
plausible allowlist entry whose ground truth is `Updates`; bulk `Updates` mail
is common enough that 6-way scoring would report the prefilter as badly wrong
when it is doing exactly its job. Its bucket is therefore reported against the
collapsed keeps-INBOX / archived matrix, with the 6-way breakdown shown
separately and read as "which category the allowlist is absorbing" rather than
as accuracy.

**`w_h` comes from labelled counts, not sampled counts.** `strata.json`
records what was *sampled*. If any message ends up unlabelled, `n_h` is
smaller than that, and weighting by the sampled figure would be wrong in
proportion to the gap. `run_eval` derives `n_h` from `labeled.jsonl ∩
sample.jsonl`; `verify` reports the difference.

### Tests

Resume skips done ids and preserves order; a correction supersedes by file
position; corrections do not cross passes; deferred messages re-present after
the main pass and not before; the shuffle is deterministic from the seed and
independent of input order; `render` truncates and marks, and never prints the
stratum; unknown category rejected on write; key-allowlist guard on
`labeled.jsonl`; cache round-trip; tolerant read of a half-written trailing
line; `verify` catches a labelled id missing from the sample or the cache, and
refuses a pass 2 that starts before pass 1 is complete.

### Verification

Label 10, `Ctrl-C`, re-run - resumes at 11, no duplicates, no lost rows. Label
one, `b`, relabel - `resolve` returns the second. `git status` shows
`eval/labeled.jsonl` and not `data/eval_cache/`.

---

## Step 4 implementation — `prompt_id` and the `PROMPTS` registry

Built 2026-09-13. A small refactor, done before step 5 because `predict()`
records `prompt_id` in the manifest and passes it to `classify()`.

```python
PROMPTS: dict[str, Callable[[tuple[Category, ...]], str]] = {"v1": build_system_prompt}
DEFAULT_PROMPT_ID = "v1"

def system_prompt(prompt_id, order=DEFAULT_ORDER) -> str
def prompt_hash(prompt_id, order=DEFAULT_ORDER) -> str    # sha256[:12] of the text
```

Three decisions worth keeping:

**`v1` IS `build_system_prompt`, not a copy of its text.** The baseline has to
be the shipped prompt or it is not a baseline, and a copy would drift the first
time either was edited.

**An unknown id raises rather than defaulting to `v1`.** The failure it
prevents is silent: a typo in a sweep that fell back would file a run under the
wrong label, and neither the results file nor the report would look wrong.

**`prompt_hash` is the backstop for the id.** `categories.DESCRIPTIONS` *is*
the prompt, so editing one line changes what every model sees. Filed under the
same id, two such runs would look comparable. A test edits a description and
asserts the hash moves.

**Registry holds `v1` only.** Variants from §5 are written immediately before
the run that tests them, so the text is fresh alongside its hypothesis and
step 4 stayed a short job. `v8-updates` was added to the §5 table during
labelling and is not yet written.

`logbook.Record.prompt_version: int | None` became `prompt_id: str | None` -
renamed, not just retyped, since `"v3-letters"` under a key called
`prompt_version` reads as a mistake and the log has no real rows in it yet.
`DESIGN.md`'s log-schema table and the comment in `categories.py` follow.

---

## Step 5 implementation — the eval harness

Built 2026-09-13. §3 stands on what the harness reports; this records how it is
put together and what §3 could not have known, having been written before the
labels existed.

### Module boundaries — see A8

```
app/evalscore.py      pure: metrics, intervals, sweeps, weighting, comparison
app/evalrun.py        predict: cache -> classify -> rows; the results file
scripts/run_eval.py   CLI: predict / score / compare
eval/results/         data only, committed
```

`evalscore` imports neither `json` nor `pathlib` - reading a results file is
`evalrun`'s job - so its purity is a property of the module rather than of
which functions happen to behave. A test parses its imports against an
allowlist, the same guard `app/decision.py` has. It *does* import
`decision.decide`: `to_action_retention` asks whether a message would have kept
`INBOX`, and a second copy of the threshold logic would measure the copy.

### The results file

Manifest as line 1, the idiom `evalset.save_frame` already uses - in the file
rather than a sidecar, because a results file that has lost its manifest is a
column of numbers whose meaning is unrecoverable. It carries `run_id`, `model`,
`prompt_id`, `prompt_hash`, `body_chars`, `extraction_version`, `orders`,
`sample_hash`, `git_commit`, `created_at` and **`n_messages`**.

`n_messages` was not in the plan. Added after the first smoke run, when a
`--limit 10` file turned out to be indistinguishable from a full run in its
manifest - a reader scoring it later would have seen n=10 with nothing saying
why.

One row per **(message, letter order)** rather than three distributions per
row, so `score()` filters by `order_name` and a run without `--permutations` is
simply a file where every row says `"default"`. Rows carry ids, probabilities
and timings; a key allowlist is checked on write, as in `evalset` and
`evallabel`.

### Decisions §3 left open

**A failed call is recorded, never re-raised and never scored as wrong.** It
gets a row with `error` set and no category, and is excluded from every
accuracy denominator. `decision.py` draws the same line for the same reason:
scoring an unreachable Ollama as a misclassification blames the model for the
network.

**Calibration buckets are half-open, so 0.8 lands in `[0.8,0.9)`.** §6 asked
for this pinned; an off-by-one moves the threshold read off the table, and that
number ends up in `DESIGN.md`.

**Wilson, not the normal approximation.** At n=140 near 0.9 the normal
approximation reports upper bounds above 1.0, and the per-stratum and
per-source breakdowns are much smaller than that.

**McNemar uses the exact binomial**, not chi-square: the discordant count is
routinely under 25, where the approximation is anti-conservative.

**`unsure` rows are reported both ways** - headline over everything, plus a
second accuracy excluding them. Nothing is hidden, and if the model's errors
cluster where the labeller was unsure that is a finding about the ceiling
rather than the model. Decided with the human; §3 predates the flag existing.

**The floor sweep skips values where `T + F >= 1`** rather than printing them
as zero, since the asymmetric rule is unreachable there and
`config._asymmetric_rule_must_be_reachable` refuses to start on such a pair.

**Permutation orders are fixed** - one rotation, one reversal - not random. Two
runs have to be comparable, and a random shuffle would make the stability
number depend on which permutation came up.

### Verification, as run

`score` on a synthetic file reproduces accuracy, recall and the confusion
matrix computed by hand (`tests/test_evalscore.py`). Then, against real Ollama:
a 10-message `predict` at `body_chars=1500` (~8.7s per call), re-scored at a
different threshold in **0.29 seconds** hitting neither Ollama nor Gmail; a
second run at `body_chars=300` (~2.2s per call); and `compare` between them
reporting 0 fixed / 1 broken. The latency difference between the two is the
truncation lever visible in one line.

---

## Step 6 results — the sweeps

Run 2026-09-13. Sixteen prediction runs, ~3,400 model calls, zero failures.
Every run is in `eval/RESULTS.md` with a one-line what-changed; the raw rows
are in `eval/results/` and re-scorable forever.

> **Read with step 7.** Every number in this section was computed against
> ground truth as it stood on 2026-09-13. Step 7's recheck corrected seven
> labels, and all fourteen runs were re-scored on 2026-09-14 — see "The
> `S_human` sweep, and what the re-score moved" under step 7, and the corrected
> table in `eval/RESULTS.md`. **The configuration selected here did not
> change**; three of the conclusions below did, and the numbers quoted in this
> section are the originals unless marked.

### The configuration this phase selected

| knob | value | how it was decided |
|---|---|---|
| model | `llama3.1:8b` | the 3B never emitted `To Action` once in 200 predictions |
| `body_chars` | **300** | the only statistically significant result in the sweep |
| prompt | **`v9b-bookings`** | best on every axis; p=0.180, *not* significant |
| `confidence_threshold` | **0.8** | measured below; the placeholder turned out right |
| `to_action_floor` | 0.15 | **inert on this model** - see below |
| `KEEPS_INBOX` | +`Bookings` | every costly error was a `Bookings` prediction |

Dev (n=140): action accuracy **0.900**, `To Action` retention **24/24**, one
message archived that should have been kept. **[re-scored 2026-09-14:** action
accuracy **0.900** and one costly error, both unchanged; retention **20/20**;
six-way accuracy 0.800 -> **0.771**.**]**

### `body_chars`: only the first 300 characters matter

`0 -> 300` is the single significant comparison in the whole phase: 20 fixed /
6 broken overall (p=0.009), and **11 fixed / 0 broken on the 24 `To Action`
messages** (p=0.001). Sender-and-subject alone catches 7 of 24 bills; 300
characters catches 18.

Above 300, nothing is distinguishable - 800, 1500 and 3000 all sit within a
handful of flips of each other and of 300 (p between 0.23 and 1.00). And 300
runs at **2.6s per call against 7.9s at 1500**, so `DESIGN.md`'s default was
costing 3x the latency for no measurable accuracy.

Underpowered but one-directional: on the `To Action` subgroup, 300 beat the
longer settings 4-0, 5-1 and 6-1. With four discordant pairs, p=0.125 is the
*smallest value the test can return*, so those cannot reach significance at
this subgroup size. Not refuted, just undetectable at n=24.

### The 3B is disqualified, and Phase 0 called it

| | `llama3.1:8b` | `llama3.2:3b` |
|---|---|---|
| accuracy | 0.743 | 0.393 |
| `To Action` recall | 14/24 | **0/24** |
| mean p(To Action) on real bills | 0.549 | 0.050 |
| median confidence | 0.982 | 0.582 |
| permutation stability | **0.850** | 0.443 |

It never predicts `To Action` at all, and its output collapses onto two
categories - 113 `Personal` against a ground truth of 16.

**Phase 0's eleven-email screen predicted this to within three points.** It
gave 9/11 = 0.818 for the 8B and 5/11 = 0.455 for the 3B; the 200-message
figures are 0.850 and 0.443. The synthetic screens were directional, as
`docs/PLAN.md` said - and directionally they were right.

### `to_action_floor` does nothing

**At F=0.15 the floor fires on zero messages.** At 0.05 it fires on two,
neither of which needed saving.

The asymmetric rule is a centrepiece of `DESIGN.md` - the one mechanism aimed
squarely at the only error that costs money - and on this model it is inert.
The cause is saturation: median confidence 0.982 and `retained_mass` 0.99997
mean the first-token distribution is nearly one-hot, so a message whose argmax
is `Receipts` essentially never carries 15% residual mass on `To Action`.
There is nothing for a floor to catch.

The protection that actually exists comes from two other places: `Needs Review`
on low confidence, and `Bookings` keeping `INBOX`. **This must be written into
`DESIGN.md`**, or the next reader assumes a safeguard that is not operating.
Keep the rule - it costs nothing and a less saturated model would make it live
again - but document it as dormant.

### `confidence_threshold`: 0.8, measured

| T | costly | clutter | review | action acc |
|---|---|---|---|---|
| 0.5 | 5 | 8 | 4% | 0.907 |
| 0.6 | 3 | 8 | 9% | **0.921** |
| 0.7 | 3 | 10 | 13% | 0.907 |
| **0.8** | **1** | 13 | 18% | 0.900 |
| 0.9 | 1 | 26 | 33% | 0.807 |

A real trade: **T=0.6 maximises action accuracy** but archives three messages
the reader wanted instead of one. The human's stated priority is that missed
mail is the intolerable error, so 0.8 is chosen deliberately at a small cost in
accuracy. Going higher buys nothing - the last remaining error sits at 0.970
confidence, so T would have to exceed 0.97, putting a third of all mail into
review.

### `Bookings` joins `KEEPS_INBOX`

Every costly error in every run was a message the model had called `Bookings`.
Three rounds of prompt work took the over-prediction from 21 of 200 down to 10
and **moved none of those messages at all**. The taxonomy change removes the
failure mode by construction rather than by persuasion: three lost messages
become zero, for five extra messages in the inbox per 140.

It is defensible on its own terms too - an upcoming flight or appointment is
something the reader wants in front of them. The cost is that the archive half
is now three categories, so "Receipts vs Bookings is a free error" - an
argument several labelling decisions leaned on - no longer holds.

### Prompt variants: three of six failed, two of them usefully

| id | result |
|---|---|
| `v2-ordered` | precedence as a numbered order. Fixed `Bookings` (21->13) but over-applied `Personal` (25 vs truth 16); clutter 17->24, action accuracy **dropped** |
| `v9-bookings` | `Bookings` description narrowed. Costly errors 6->3 at no extra clutter |
| **`v9b-bookings`** | v9 minus the word "appointment", `Personal` exclusion moved *inside* the description. Best overall |
| `v8-updates` | **failed.** Dropped "low-priority"; costly errors 6->7, `Updates` predictions unchanged at 6 (truth 21) |
| `v4-format` | **failed.** Named the input fields and stated the From address is not a category |

Two failures worth keeping:

**`v8-updates`** shows the "low-priority" wording was a *labelling* problem,
not a model one. It made the human's judgement harder without affecting the
model's - a distinction that would have been invisible without the eval.

**`v4-format`** is the more interesting failure. Both messages it was written
for - a flight e-ticket from `Receipts@united.com` called `Receipts`, an unpaid
invoice from `bookings@anaesthesia-analgesia.com.au` called `Bookings`, both
above 0.94 - were **unchanged**. Its apparent win of zero costly errors was an
artefact: the calibration gap fell from +0.162 to +0.102, pushing seven more
messages below the threshold into `Needs Review`. Fewer things were archived
because the model hedged, not because it understood. Worth remembering as a
pattern: *a metric improving because confidence dropped is not an improvement.*

**`v9b` is not statistically significant either** - 7 fixed / 2 broken against
v1, p=0.180. What supports it is a pre-registered mechanism (`Bookings`
over-prediction) whose intermediate quantity moved as predicted, twice: 21 ->
13 -> 10 against a truth of 6.

### The soft spot to watch on the holdout

**[re-scored 2026-09-14]** `To Action` recall is 14/20 overall but **5/9 on
`draw == "R"`**, the uniform draw, against 9/11 on the keyword-mined strata.
The `R` figure did not move at all under the corrections - every message that
left the class was mined - so the soft spot is exactly as it was, and is now a
larger share of a smaller denominator. Retention covers it at 9/9 - but that is
`Needs Review` and `KEEPS_INBOX` doing the work, not the model recognising a
bill. The mined figure is flattering because those messages say "overdue" on
the tin.

### Dev exposure

Six prompt variants and a threshold sweep were selected against the same 140
dev messages, three of the prompt rounds tuned by reading specific errors. The
0.800 is optimistic by an unknown amount and the holdout is the only instrument
that can say how much. This is the reason step 8 opens the box **once**.

### Still open

- **Merging `Promotions` and `Updates`.** 14% of remaining errors, all free
  (both archive). Cheap version: merge only the applied Gmail label, leaving
  the six-way enum and every measurement intact. Decision is whether the
  reader would ever bulk-delete one and not the other. **Left open
  deliberately at the end of Phase 2** - it is a preference about how the
  archive reads, not a measurement, and live use answers it better than the
  eval set can. `DESIGN.md` already lists it under Future Enhancements.
- ~~**`DESIGN.md` updates** - `body_chars`, `T`, the dormant floor, and
  `KEEPS_INBOX` - deliberately deferred to step 8, after the holdout.~~
  **Written 2026-09-14**, along with the model-selection section, the category
  table, the action-space argument and the measured-configuration table.
- **`Personal` recall is 4/14 on dev, every error free.** Closed for this
  phase with the measurement behind it - see "No prompt round for `Personal`"
  under step 7. Revisit only if the shelf proves unreliable in live use, and
  score it against a holdout that has not been spent.

---

## Step 7 implementation — the blind recheck

Built 2026-09-13, before the sitting. `recheck` re-presents a subset of the
sample with the pass-1 label hidden and reports how often the labeller
reproduces their own decision. That rate is the ceiling: at 27/30 no
classifier can score above ~0.90 against this ground truth however good it is,
and the dev 0.800 reads as ~0.89 of what is achievable rather than 0.80 of a
perfect target.

**Pre-registered, before the number exists**, because the temptation to read it
generously arrives with the result:

| outcome | reading |
|---|---|
| agreement ≥ 0.90, disagreements on `unsure` rows | ceiling is sound; the flagged rows are the known noise |
| agreement < 0.85 | the labelling rules are not deterministic enough — ground truth needs a rules pass before step 8 |
| any disagreement crossing `KEEPS_INBOX` | matters more than the raw count: that is the ceiling on action accuracy, the metric with consequences |

And the honest limit: **at n=30 the interval is roughly ±0.11**. 27/30 gives
[0.74, 0.97]. This can separate "the rules are reproducible" from "ground truth
is noisy" and it cannot separate 0.90 from 0.98, so the figure goes into
`DESIGN.md` at step 8 as an interval, never as a point.

### Two ceilings, not one

The harness reports two headline numbers, so the recheck reports two:
`n_agree / n` bounds `accuracy`, and agreement collapsed through `KEEPS_INBOX`
bounds action accuracy. Post-A-Bookings that collapse is
`{To Action, Personal, Bookings}` against `{Receipts, Updates, Promotions}`, so
a `To Action` -> `Personal` flip agrees on the action while
`Bookings` -> `Receipts` does not.

**Cohen's kappa was rejected.** It corrects raw agreement for chance using the
estimated marginals, and at n=30 over six categories those marginals are
noisier than the statistic they adjust. Raw agreement is also the quantity that
directly bounds accuracy, which is the whole point of measuring it.

### Which 30

Uniform over all 200: `Random(99)` shuffle of the sorted sample ids, first `n`.

- **Not stratified by the pass-1 label.** It would over-represent the rare
  categories and bias the headline in a direction nothing downstream could
  correct for. The cost is a thin per-category breakdown, and the seed-99 draw
  in fact contains no `Bookings` — 6 of 200 means a 40% chance of that, and
  re-drawing until one appears would be choosing the draw by looking at the
  labels, which is the bias the uniform draw exists to avoid.
- **Not dev-only.** The procedure was applied identically to both splits, and
  step 8 reads the holdout against the same ceiling. The draw came out 21 dev /
  9 holdout, all four strata, 2 of the 6 `unsure` rows.
- **Shuffle-then-take**, so the draw has the prefix property A9 relies on.

### Blindness is structural where it can be, procedural where it cannot

The loop never reads a pass-1 record, and `render` takes a `Cached`, which
cannot carry a label — the same argument that keeps the stratum off the screen.
The part that needed a decision is the *report*: it prints **only once the draw
is complete**. Learning at message 10 that you have already disagreed twice
changes how carefully you judge messages 11 to 30, so a partial sitting prints
progress and nothing else, and `recheck --report` prints the comparison later.

`--body-chars` defaults to 1500, matching pass 1. A different value would
measure a different view of the message rather than the labeller.

### Signatures

```python
# app/evallabel.py — pure
RECHECK_PATH = EVAL_DIR / "recheck.json"     # committed, ids only
RECHECK_PASS, DEFAULT_RECHECK_N, DEFAULT_RECHECK_SEED = 2, 30, 99

@dataclass(frozen=True)
class RecheckDraw:  n; seed; created_at; message_ids
@dataclass(frozen=True)
class Disagreement: message_id; first; second; unsure; crosses_inbox
@dataclass(frozen=True)
class Agreement:    n; n_agree; n_action_agree; n_unsure; n_unsure_agree;
                    disagreements

def recheck_draw(sample_ids, n, seed) -> list[str]
def agreement(first, second) -> Agreement
def save_draw(draw, path) -> None
def load_draw(path) -> RecheckDraw | None
```

Three decisions inside those:

**A pass-2 row with no pass-1 row raises.** That condition means
`labeled.jsonl` is damaged, and skipping it would shrink the denominator and
flatter the ceiling by exactly the rows lost.

**An unreadable draw file raises, where an unreadable cache entry reads as
absent.** The cache is derived data and refetching costs a second; the draw is
the measurement plan, and treating damage as "never drawn" would replace it
with a fresh one mid-recheck.

**The Wilson interval comes from `evalscore.wilson`**, imported rather than
rewritten — one estimator, one implementation. `evallabel` gains no new
dependency edge that matters: `evalscore` is the pure module and imports
nothing it should not.

### What the CLI grew

`recheck [--n 30] [--seed 99] [--body-chars 1500] [--limit] [--batch]
[--report]`, refusing to start until `unfinished(records, sample_ids, 1)` is
empty — the guard step 3 specified, using the function it already added. Fully
offline: all 200 messages are cached, so `gmail()` is never called.

The keypress loop was **extracted from `cmd_label` rather than copied** into
`label_loop(queue, resolved, *, pass_no, ...)`. A copy would be the code that
writes ground truth, and two of those drift — `b` fixed in one and not the
other is a misfire that survives into the labels.

`verify --pass 2` was **wrong before this step** and is fixed here: it compared
pass 2 against all 200 sample ids, so a 30-message recheck would have reported
170 messages missing. It now checks against the pinned draw, and drops the
`w_h` column for pass 2, since that weight is a pass-1 quantity `run_eval`
computes from pass 1 and nothing weights the recheck by.

### Tests — 21 new, 393 total

Draw: deterministic from the seed; independent of input order; a subset with no
repeats; `n=40` extends `n=30`; an over-large `n` is the whole sample.
Agreement: exact matches; measured only over the rows pass 2 reached; the free
error agrees on the action where the costly one does not; `unsure` in either
pass attributed; an orphan pass-2 row raises. Draw file: round-trips; carries
only `RECHECK_KEYS`; absent is `None` and damaged raises.

### Verification, as run

Driven end to end against a **copy** of `eval/` in a scratch directory, with 30
synthetic keypresses derived from the pass-1 labels and three deliberate flips
— one crossing `KEEPS_INBOX`, one on an `unsure` row, one free. Reported 27/30
[0.74, 0.97] 6-way and 28/30 [0.79, 0.98] on keeps-INBOX, named all three
disagreements with sender and subject, and `verify --pass 2` came back `OK`
against the pinned draw. Then: `--report` reprints without labelling, `--n 35`
extends the draw and resumes at [31/35] with the recorded 30 standing, `--seed
7` and `--n 20` are both refused. Nothing in the real `eval/` was touched — the
draw there is unpinned until the sitting starts.

### Step 7 results — the ceiling, measured 2026-09-14

Written down **before** any correction was applied, because a `--relabel` makes
pass 1 agree with pass 2 by construction and `recheck --report` would then
print a different, wrong number from the same draw.

| ceiling | agree | rate | 95% CI |
|---|---|---|---|
| 6-way, bounds `accuracy` | 28/30 | 0.933 | [0.79, 0.98] |
| keeps-INBOX, bounds action accuracy | **30/30** | **1.000** | [0.89, 1.00] |

`unsure` in either pass: 2/30, both agreed.

**The operational ceiling is clean.** Thirty of thirty judgements were
reproduced on the axis that decides whether a message is archived, so the dev
action accuracy of 0.900 is measured against a target with no detectable noise
in the dimension that costs anything.

**Both six-way flips are one rule, one direction, one stratum.** `To Action` ->
`Personal` on two human-written replies, both in `S_human`: an AGSVA clearances
officer and a KHOO accounts reply. That is the **precedence rule** - a human
wrote it, so `Personal`, even when the message also demands something - which
was itself refined *during* labelling, so early rows predate it. Not labeller
noise but a rule applied unevenly: the cheap kind, visible and fixable, exactly
as the step 3 rules section predicted it would appear.

Both flips are free by construction, which is why keeps-INBOX is 30/30. §3's
labelling rules called this cell in advance: "`Personal` vs `To Action` is
therefore a free error on any human-written message."

**Against the pre-registration, honestly: only half of it holds.** Agreement
cleared 0.90, but the disagreements were *not* on the `unsure` rows - both of
those were reproduced, and both flips came from rows the labeller was confident
about twice. At n=2 that is barely evidence, but the flag did not locate where
ground truth was soft, and it was expected to.

### The `S_human` sweep, and what the re-score moved

The two flips were not the whole of it. Nine `S_human` rows carried
`To Action`; the precedence rule was reapplied to all nine together, thread
mates adjacent - pass 1 randomised its order so repeated judgements would not
drift together, but this was a rule-application sweep rather than fresh
judgement, and there consistency is the point. **Seven moved to `Personal`**:
both AGSVA human replies, the KHOO invoicing reply, the casting call, and
three more. Two stayed `To Action` - the mailer-daemon delay/failure pair, per
"something you initiated that has not completed" - as did the AGSVA
`donotreply` overdue notice, the KHOO invoicing *opener* and the Payoneer auto
reply, all three automated. The thread split is the case §3 already
contemplates: a human's reply is `Personal` while the platform's own message in
the same thread is not.

`To Action` 33 -> 29, `Personal` 16 -> 20. All fourteen runs re-scored - free,
no re-inference, `eval/results/` untouched.

| dev metric, `v9b-bookings` | before | after |
|---|---|---|
| accuracy | 0.800 | **0.771** [0.70, 0.83] |
| `To Action` recall | 18/24 | 14/20 (`R` draw 5/9, unchanged) |
| `To Action` retention | 23/24 | **20/20** |
| action accuracy | 0.900 | **0.900** |
| costly / clutter | 1 / 13 | **1 / 13** |
| calibration gap | +0.162 | +0.157 |

**The action matrix did not move by a single message.** Every corrected row was
a `To Action` -> `Personal` move, both sides of which keep the inbox, so the
six-way number absorbed the entire cost while the metric with consequences was
untouched. That is the free-error property of the taxonomy, measured rather
than argued.

**Three step 6 conclusions changed, and none of them changed the decision:**

1. **`0 -> 300` is no longer significant overall** - p=0.009 became p=0.064.
   What survives is the subgroup that matters: **8 fixed / 0 broken on the 20
   `To Action` messages, p=0.008**. The claim narrows from "the body lifts
   accuracy" to "the body catches bills", which is the claim worth having.
2. **`v2-ordered` now has the highest six-way accuracy of any run** (0.779
   against `v9b`'s 0.771) and is still rejected: clutter 24 against 13, action
   accuracy 0.821 against 0.900. The clearest demonstration in the phase that
   six-way accuracy is not the metric that decides.
3. **Every prompt `p`-value got weaker** - `v9b` vs `v1` is 6 fixed / 3 broken,
   p=0.508, and `v9b` vs `v9` is 3/3, p=1.000. The selection rests entirely on
   the pre-registered `Bookings` mechanism and on action accuracy, with no
   support at all from significance. Step 6 already said the effect size was
   not significant; it is now less so.

`v9-bookings`' supporting note that `Personal` "lands exactly on 16" is
withdrawn - truth is 20. `eval/RESULTS.md` carries the corrected table.

### No prompt round for `Personal`, and the measurement that settles it

The recheck raised the obvious question - the model reads the precedence rule
poorly, so should the prompt say it louder? Measured on dev with all
corrections applied: true `Personal` 14, predicted correctly **4**, mean
`p(Personal)` on true `Personal` 0.302. Genuinely weak.

**And every one of the 10 misses is free**: 6 to `To Action`, 4 to `Bookings`,
all three in `KEEPS_INBOX`. **No human-written message was archived on dev at
all** - zero `Personal` -> `Receipts`/`Updates`/`Promotions`. The failure is a
shelf label, not a lost email.

The upside is bounded and free - fixing all 10 moves six-way accuracy 0.771 ->
0.842 and action accuracy by exactly zero - while the downside crosses
`KEEPS_INBOX`: the model already predicts `Personal` on 7 dev messages of which
2 are true `Updates`, bulk mail pulled into the inbox. And the experiment has
already been run. **`v2-ordered` is precisely this change**: precedence as an
explicit numbered order took `Personal` predictions to 25 against a truth of
16, clutter 17 -> 24, action accuracy down. A measured failure, not a
hypothetical one.

Three further reasons, recorded so the question does not get reopened by
instinct: the dev set has already absorbed six variants and a threshold sweep,
and a seventh round aimed at the cheapest prize spends what is left of it; the
`To Action` / `Personal` boundary is where the *human* wobbled twice in 30, so
tuning toward it is tuning toward noise; and the taxonomy already covers the
operational need, since `Personal` mail stays in the inbox under whichever of
the three labels it lands on.

**What would reopen it:** human-written mail predicted `Receipts`, `Updates` or
`Promotions`. That crosses into the archive, and a missed human email is a real
loss. There are none on dev.

### The policy on disagreements, agreed before the sitting

Where pass 2 exposes a genuine pass-1 error, **fix it** with `label --relabel
<id>` and re-run `score` on the affected runs. Leaving a known-wrong label in
ground truth to protect comparability is the wrong trade with step 8 still
ahead, and re-scoring is free — no re-inference, since model, prompt and
`body_chars` are unchanged, so the cost is seconds and a `RESULTS.md` edit.

The ceiling is recorded **as measured before** those fixes and is not
re-measured from the same 30: the draw has been seen now, so a second pass over
it would measure memory. If the ceiling needs narrowing later, `--n 40` extends
into unseen messages.

---

## `v10-itinerary` — the round step 7 made necessary

Written and **pre-registered 2026-09-14, before the run**. A seventh prompt
variant against the same dev 140, which adds to the exposure recorded above and
is taken deliberately: unlike the `Personal` question, this one targets the
only error class in the system that costs anything.

### Why the existing prompt is inconsistent with the taxonomy

`v9` and `v9b` narrowed `Bookings` because, **while `Bookings` archived**,
every costly error in the phase was a `Bookings` prediction. Step 6 then moved
`Bookings` into `KEEPS_INBOX`, which reverses the asymmetry - and the prompt
never caught up. Measured on `20260913T113909-f1f3a4`, dev:

| | |
|---|---|
| true `Bookings` | 5 |
| predicted `Bookings` | 8, of which 3 correct |
| the 5 false positives | 4 true `Personal`, 1 true `To Action` |
| clutter errors caused by a `Bookings` prediction | **0** |
| the 2 misses | United e-ticket -> `Receipts` (**the run's only costly error**), a wedding countdown -> `To Action` (free) |

**Over-predicting `Bookings` now costs nothing**, because its false positives
land on messages that keep the inbox anyway; **under-predicting it costs the
only consequential error in the run.** Narrowness is optimising against the
current cost function.

### The mechanism, and why it is in the description

```
Qantas  "Confirmation and E-Ticket Flight Itinerary"  -> Bookings  0.991
United  "eTicket Itinerary and Receipt for Conf..."   -> Receipts  0.970
```

Same genre, opposite answers, so the model is keying on the literal token
*receipt* - in the subject and in `Receipts@united.com` - rather than on the
itinerary. The precedence block already says "`Bookings` over `Receipts`" and
did not help, because at `p(Bookings) = 0.027` the model never had `Bookings`
in play for precedence to arbitrate. The change therefore goes in the
**description**: one added sentence, *a ticket or itinerary for a trip that has
not happened yet belongs here even when the same email is also the receipt for
it*. One idea, so the result is attributable.

### Pre-registered decision rule

**Primary, the mechanism:** the United e-ticket flips to `Bookings`, or
`p(Bookings)` on it rises materially from 0.027.

**Guards - all must hold, or `v9b` stands:**

| guard | `v9b` | required |
|---|---|---|
| action accuracy | 0.900 | >= 0.900 |
| costly errors | 1 | <= 1 |
| `To Action` retention | 20/20 | 20/20 |
| clutter | 13 | <= 13 |
| dev `Bookings` predictions | 8 | <= 12 |

The last guard is the failure mode worth naming in advance: the new sentence
could pull true `Receipts` across, and `Receipts` archives, so that direction
**would** create clutter where the current false positives do not.

**And the `v4-format` lesson applies:** if the error count improves while the
calibration gap falls, that is the model hedging into `Needs Review`, not
understanding. Not an adoption.

**What cannot be measured:** dev holds 5 true `Bookings` and the holdout 1, so
`Bookings` recall has no useful precision here and no claim will be made from
it. The evidence is the named message plus the prediction-count guards - the
same standard `v9` and `v9b` were judged by.

### Result — rejected, 2026-09-14

Run `20260914T132822-bb1948`. **`v9b` stands.**

| criterion | `v9b` | `v10` | |
|---|---|---|---|
| six-way accuracy | 0.771 | **0.800** (4 fixed / 0 broken, p=0.125) | looks like a win |
| **action accuracy** | **0.900** | **0.857** | **fails the guard** |
| costly errors | 1 | 0 | passes |
| clutter | 13 | **20** | **fails the guard** |
| `To Action` retention | 20/20 | 20/20 | passes |
| `Bookings` predictions | 8 | 9 | passes |
| calibration gap | +0.157 | +0.131 | the hedging signature |

**The target message never flipped.** United is still `Receipts` - but at
**0.752 rather than 0.970**, which puts it under T=0.8 and into `Needs Review`,
where it keeps the inbox. Costly errors reached zero because the model became
uncertain, not because it became right.

**The mechanism was real and insufficient.** `p(Bookings)` on that message rose
**0.027 -> 0.238**, ninefold. The sentence is read, and it moves mass in the
intended direction on the intended message. It cannot overcome the literal
token *receipt* in the subject and in `Receipts@united.com`.

**The cost landed somewhere the pre-registration did not predict.** `Bookings`
predictions barely moved (8 -> 9), so the clause did *not* pull true `Receipts`
across as the guard anticipated. Instead it destabilised the whole `Receipts`
class: `Needs Review` went 17.9% -> 25.7% and clutter from `Receipts`
predictions went 2 -> 8. Uncertainty keeps the inbox, so hedging shows up as
clutter.

**Why this is the phase's best argument for the collapsed matrix.** Six-way
accuracy rose one-directionally - 4 fixed, 0 broken, nothing traded - and the
metric with consequences fell. Adopting on the headline number would have
bought zero costly errors at the price of seven extra pieces of inbox clutter
and a worse action matrix. `v2-ordered` made the same point less sharply,
because there the six-way number and the action number moved together.

**Recorded against the `Bookings` question, which was well posed:** the prompt
*was* inconsistent with the taxonomy after `Bookings` joined `KEEPS_INBOX`, and
narrowness *was* optimising against the old cost function. The description
route was the right thing to try and it is now measured: it shifts the target
distribution ninefold and still loses to one token. The residual exposure
stays what step 7 recorded - a booking that looks like a receipt - and it
should be written into `DESIGN.md` at step 8 as a known, measured limit rather
than an open question.

## Step 8 — the lock-box

### Pre-registration, written 2026-09-14 before the holdout was scored

**The run:** `20260913T113909-f1f3a4` - `llama3.1:8b`, `v9b-bookings`,
`body_chars=300` - scored at **T=0.8, F=0.15** on `--holdout`, n=60. One call.
`score` prints a threshold sweep as part of its output; **T is not re-chosen
here.** 0.8 was measured on dev, and picking a threshold off the holdout table
would turn the holdout into a second dev set, which is the exact failure the
split exists to prevent.

**Why the estimate is expected to be optimistic.** Seven prompt variants, a
five-point `body_chars` sweep and a threshold sweep were all selected against
the same dev 140, three of the prompt rounds tuned by reading specific errors.
The dev figures are biased upward by an unknown amount and the holdout is the
only instrument that can say by how much. A drop of a few points is the
expected outcome, not a failure.

**What the holdout can answer**, at n=60 with 16 keeps-INBOX truths against 44
archives: overall accuracy against dev's 0.771, and the action matrix -
costly errors and clutter - which is the pair the gate turns on.

**What it cannot answer, registered now so the result is not over-read.** The
holdout holds **9 true `To Action`**, of which 3 are `R`-draw:

| recall | Wilson |
|---|---|
| 9/9 | [0.70, 1.00] |
| 8/9 | [0.56, 0.98] |
| 7/9 | [0.45, 0.94] |

Those intervals overlap almost entirely, so **no claim will be made from
holdout `To Action` recall**, and the `R`-draw soft spot (5/9 on dev) is
untestable at n=3. Gate question 2 is answered by retention and by the dev
recall, with the holdout figure reported for completeness only. The single
true `Bookings` on the holdout means the same applies there.

**Read against the ceiling, not against 1.0:** 28/30 [0.79, 0.98] six-way and
30/30 [0.89, 1.00] on keeps-INBOX, from step 7.

**Decision rule, agreed before looking:**

- **Pass** if the action matrix holds up - costly errors at most 2 of 16
  keeps-INBOX truths, and overall accuracy within the dev interval [0.70, 0.83].
- **Fail** if action accuracy lands materially below dev, under 0.83. In that
  case the honest response is to draw a **fresh** holdout by extending the
  sample, **not** to iterate against the one just spent. Tuning against a
  revealed holdout is precisely what the lock-box exists to prevent, and the
  temptation would be at its strongest at that exact moment.
- Either way the number is recorded with its interval, and `DESIGN.md` is
  written from the dev-measured configuration, which is already fixed.

### The holdout, opened once — 2026-09-14

`20260913T113909-f1f3a4`, `--holdout`, T=0.8, F=0.15, n=60.

| | dev (n=140) | **holdout (n=60)** |
|---|---|---|
| accuracy | 0.771 [0.70, 0.83] | **0.800** [0.68, 0.88] |
| weighted | 0.771 | 0.787 |
| excluding `unsure` | 0.774 | 0.825 |
| **action accuracy** | **0.900** | **0.900** |
| costly errors | 1 of 39 keeps | **1 of 16 keeps** |
| clutter | 13 | 5 |
| `To Action` retention | 20/20 | **9/9** |
| `To Action` recall | 14/20 | 6/9 *(no claim - see pre-registration)* |
| calibration gap | +0.157 | **+0.166** |

**Pass on every pre-registered criterion**: costly errors 1 (rule: at most 2),
accuracy 0.800 inside the dev interval [0.70, 0.83], action accuracy 0.900 (rule:
at least 0.83).

**No detectable inflation** - and the honest version of that claim. The point
estimate went *up*, which is the opposite direction from overfitting, and the
action matrix is identical to three decimal places. But n=60 gives [0.68, 0.88],
which overlaps the dev interval almost entirely, so what this licenses is
"inflation is not detectable at this sample size", not "there is none". The
best available reading is that selection ran mostly on mechanism rather than on
error-fishing - `v9b` was adopted on a pre-registered `Bookings` mechanism, and
`v8`, `v4` and `v10` were each rejected on guards rather than adopted on noise.

**The floor is dormant here too.** The holdout floor sweep returns an identical
9/9 kept and 40 archived at F = 0.05, 0.10 and 0.15. Step 6 found this on dev;
it now replicates on data no tuning ever touched, which upgrades it from an
observation to a property of this model.

### The one costly error, and the finding it forces

```
199b6758f4fcd5ad   S_human, mined
from: Paula from Checkmate <team@checkmate.tech>
subj: Re: Background Checking | EY | Membership Verification
truth Personal -> predicted Promotions at 0.925 -> archived
```

A human-written reply, archived as marketing. **This is exactly the condition
registered under "No prompt round for `Personal`" as the thing that would
reopen that question**: *human-written mail predicted `Receipts`, `Updates` or
`Promotions`. That crosses into the archive, and a missed human email is a real
loss.* On dev there were none; on the holdout there is one, so across all 200
messages one human-written email would be archived.

**And it cannot be acted on now.** Tuning a prompt in response to a holdout
error spends the holdout, which is the single thing the pre-registration
forbids and the reason the box is opened once. The correct disposition is to
carry it into Phase 3 as a **stated hypothesis with a stated test**: if
`Personal` -> archive appears in live use, that is the trigger for a `Personal`
prompt round, scored against a *fresh* holdout drawn by extending the sample.
Recorded in `docs/PLAN.md` under Phase 2 findings, not fixed here.

Note it is also the reverse of the dev picture, where every `Personal` error
was free. One message is not a pattern; it is enough to move the question from
"closed" to "watch", which is what it has been set to.

## Open question carried into this phase

**Refresh-token lifetime in Testing status** (from Phase 0). Assumed 7 days;
the current consent dates from 2026-09-08 13:41 UTC, so the assumption
predicts expiry around 2026-09-15. Labelling is a 1.5–2 hour sitting, so this
only bites if it starts late in the window. When auth does break, record the
actual interval in `docs/PLAN.md` — it resolves the last Phase 0 spike
question for free.
