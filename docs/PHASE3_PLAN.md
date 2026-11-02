# Phase 3 — The writer

Companion to `docs/PLAN.md`, which says what Phase 3 contains and what has to
be true before Phase 4. This document says *how* it gets built.

**Start at [Build order](#build-order).** It says which step is current and
what each finished one produced. Then read the amendment log, which lists every
place the build knowingly departs from `docs/PLAN.md` → Phase 3.

How to read the rest: the numbered sections are the plan as agreed in the
planning session of 2026-10-22. Each step gets a more detailed
`Step N implementation` section, written just before it is built, near the
end. Where the two disagree, the step section is newer.

> **Status: SIGNED OFF 2026-11-02.** Agreed in outline 2026-10-22; open
> points settled at plan review on 2026-11-02 (see the second decisions
> table).

---

## Amendment log

**A1 — `messages.modify` lands before `undo_run.py`.** `docs/PLAN.md` orders
undo (its step 4) before the write call in `gmail_client` (its step 5). Undo
*is* a write, so it needs that call to exist first. Swapped. The safety rule
still holds, and is now precise: **until the undo rehearsal has passed,
`undo_run.py` is the only caller of `modify_labels`.** `agent.py` is not
written until after it.

**A2 — The write interface and the repo-wide guard come first.** Not a
numbered step in `docs/PLAN.md`. Found during planning: the existing
`test_gmail_client_contains_no_write_call` scans only `app/gmail_client.py`,
while `scripts/measure_bodies.py` already calls `svc.users()` directly and
`scripts/spike_gmail.py` builds its own client. On `gmail.readonly` that hole
does no harm. On `gmail.labels` or `gmail.modify` any script could write, and
no test would notice. It is closed before the scope widens, not after.

**A3 — Undo is driven by the log, not by a Gmail date query.** `DESIGN.md` →
Dry-run mode bounds undo with Gmail's `after:` epoch. That filters on when a
message was *received*, not when the agent *labelled* it: a 2023 message
labelled by a backfill today falls outside `after:today`. Also, the backfill
touches mail that is already archived, so a blind "restore `INBOX`" would
un-archive mail the reader archived by hand. As planned: the log records each
message's labels before the write, the effective change actually made, and
the time it was made. Undo selects rows by log timestamp and applies the exact
inverse. The `STOP_LABELS` sweep stays as a cross-check. The required window
with no default is unchanged; only what it is measured on changes.
`DESIGN.md` to be amended when step 6 lands.

**A4 — A review tool for the large dry run.** `docs/PLAN.md` asks for the
large dry run to be "read as a second eval" but lists no tool for reading it.
Reviewing a few hundred predictions by grepping a JSONL file would not be
reliable, so it gets a step (step 9) of its own.

---

## Context

Phase 2 closed on 2026-09-14 with the gate passed. What it hands over:

- **The shipped configuration:** `llama3.1:8b`, prompt `v9b-bookings`,
  `body_chars=300`, `T=0.8`, `F=0.15` (dormant on this model).
  `KEEPS_INBOX = {To Action, Personal, Bookings}`.
- **Measured quality, including the reply rule:** action accuracy 0.900 dev /
  0.917 holdout, `To Action` retention 29/29 across all 200, one of 55 wanted
  messages archived. The dev set absorbed six prompt variants and three
  sweeps; the holdout is spent.
- **The read half:** `gmail_client` (search, fetch, MIME walk, backoff,
  `throttle`) and `message_body`. Every Gmail call to date is a read.
- **The decision core**, including `decide_reply_hit()`. The reply rule's pure
  half exists. Its call site and the `logbook` `Source` value `"reply"` do
  not.
- **Two watch items with stated triggers**:
  - `Personal` → archive in live use triggers a `Personal` prompt round
    against a *fresh* holdout.
  - Rule-routed `Personal` filling with unwanted auto-responders triggers the
    `List-Unsubscribe` refinement (`docs/BACKLOG.md`).

  The large dry run is the first instrument for both.

The outcome this phase produces: the first write access, a rehearsed undo, a
second eval on mail no tuning has seen, and one small live run that is
reverted cleanly.

---

## Decisions taken (planning session, 2026-10-22)

| Decision | Choice |
|---|---|
| Scope sequence | `gmail.readonly` → `gmail.readonly` + `gmail.labels` → `gmail.modify`. Each widening is its own step and its own consent |
| Granted vs requested scope | Checked against what Google **granted**, not the `SCOPES` constant. Each entry point asserts the scope it needs |
| Write interface | `gmail_client` is the only module that touches the Gmail API. Exactly two write functions: `create_label` and `modify_labels`. No rename, delete or trash function exists |
| Write guard | Repo-wide test over `app/` and `scripts/`: no write call except those two exact call chains in `gmail_client` |
| Label ids | Resolved once per run via `labels.list`. **Fail closed** if any `Agent/` label is missing |
| Parent `Agent` label | Measured in step 3. Gmail labels are flat names; nesting is display only |
| Undo | Log-driven: `prior_labels` + effective delta + log timestamp. Exact inverse. Label sweep as cross-check (A3) |
| Log vs modify order | Log row written **after** `modify`, carrying success or `error`. An append that fails after a successful modify is the orphan case the sweep catches |
| Failure in Phase 3 | No labels, a log row with `error`. **One failure never becomes `Agent/Error`**. Dead-lettering is Phase 4 |
| CLI dry run | Dry by default. Writes need an explicit `--live`. `--limit` is required, and live runs are capped by a constant that Phase 4 removes |
| Large dry run review | Every message reviewed, **blind**: the prediction is hidden (changed from "prediction shown" at plan review, 2026-11-02). Reviewer's own call. Gives both the action matrix and a six-way figure |
| Re-consent | Every 7 days is normal operation in Testing status, not a bug. The step 2 consent date and the date it actually fails are recorded |
| Out of scope → Phase 4 | Dead-lettering, Needs Review reconciliation, `batchModify` bucketing, the poll loop |
| Dependencies | None new. Everything is `google-api-python-client`, already present |

### Decisions taken (plan review, 2026-11-02)

| Decision | Choice |
|---|---|
| `scripts/spike_gmail.py` | Kept, exempt from the write guard by exact path. Stated in the test and in the script's header (§1) |
| `LIVE_LIMIT_CAP` | 25 |
| Review tool (step 9) | Agreed as written. Output not committed |
| Large dry run draw | Most recent 300 after 2026-09-11 |
| Gate thresholds | Action accuracy ≥ 0.84; costly errors ≤ 2% of keeps-`INBOX` truth |
| `OllamaError` retries | 4 retries after the first attempt, 5 tries in total |
| Review blindness | Blind. Seeing the prediction anchors the reviewer toward agreeing, which would inflate accuracy against a dev figure that was measured blind |

---

## 1. The write interface and the guard (A2)

### `app/gmail_client.py` — the only door to the API

Two write functions, added at different steps so each scope step's code can
do only what that scope allows:

```python
def create_label(svc, name: str) -> str:
    """Create one user label, returning its id. Step 1."""

def modify_labels(
    svc, message_id: str, *, add_ids: Collection[str], remove_ids: Collection[str]
) -> None:
    """The single atomic label change. Step 5."""
```

- **`modify_labels` takes both sets in one call, by design.** Splitting an
  add from a remove is not possible through the interface, so the
  atomic-modify rule in `CLAUDE.md` is enforced by the signature rather than
  by convention. Raises on an empty change, which is a caller bug and should
  not be a silent no-op.
- **It takes ids, not names.** Name-to-id resolution is a read and lives with
  the reads:

  ```python
  def label_ids(svc, names: Collection[str]) -> dict[str, str]:
      """name -> id for every requested label. Raises if any is missing."""
  ```

- `messages.modify` is idempotent (it applies sets), so the existing
  `_execute` backoff is safe to wrap around it: a retried modify that had
  already succeeded changes nothing. A 403 for insufficient permission is
  still never retried (`_retryable`). That is now the error that matters
  most.

**Rejected: a wrapper class around the service object.** It looks more
structural, but Python cannot hide the object it wraps, so a test would still
be the real enforcement. It would also mean rewriting how every Phase 2
script passes `svc` around. Scope is the backstop underneath. `gmail.modify`
can trash a message but cannot delete one permanently, so `trash` is the
worst write the code guard has to stop.

### `tests/test_write_guard.py` — repo-wide, replacing the per-module test

Parses every `.py` file under `app/` and `scripts/`, and fails if:

1. any module other than `gmail_client` imports `googleapiclient`, or
2. any call's method name is in the write set (`modify`, `batchModify`,
   `trash`, `untrash`, `delete`, `batchDelete`, `insert`, `import_`, `send`,
   `create`, `update`, `patch`) **unless** it is one of the allowed
   `(file, resource, method)` triples:
   - step 1: `("app/gmail_client.py", "labels", "create")`
   - step 5: `("app/gmail_client.py", "messages", "modify")`, added
     deliberately in that step's diff

The resource name is read from the receiver chain (`…labels().create(…)`),
which is what lets the test tell `labels().create` apart from
`drafts().create`.

`test_gmail_client_contains_no_write_call` is replaced by this, not deleted
silently: the new test covers a strict superset and its docstring says so.

Consequences for existing code:
- `scripts/measure_bodies.py:219` is switched to `gmail_client.fetch`-level
  helpers (a raw `messages().get` that returns the payload, since it needs
  more than `Message` carries).
- `scripts/spike_gmail.py` imports `googleapiclient` and builds its own
  client. **Decided (2026-11-02): kept, and exempt from the guard.** The
  exemption is by exact path, `scripts/spike_gmail.py`, not a `spike_*.py`
  glob, so a new spike file is caught rather than waved through. It is stated
  in two places: the guard test's exemption list (with a comment explaining
  why), and a header comment at the top of the script saying it is
  exempt from `tests/test_write_guard.py`, builds its own client, and must
  stay read-only. The cost is accepted: this file could reach the API
  through the side door, and only reading it would show that. Scope is
  still the backstop underneath.

### Granted-scope check — `app/auth.py`

```python
def granted_scopes() -> frozenset[str]:
    """What the stored token actually carries, as Google returned it."""

def require_scope(scope: str) -> None:
    """Raise NotAuthenticated-style if the granted set does not include it."""
```

`status()` reports the granted set rather than `SCOPES`. **To verify while
building:** where `google-auth-oauthlib` surfaces the token response's
`scope` field. `Credentials.from_authorized_user_info(info, SCOPES)` sets
`.scopes` to what was *requested*, which is the very confusion being fixed.
If the granted set is not persisted today, `_write_token` starts storing it.

`gmail.modify` includes everything `gmail.labels` and `gmail.readonly` can
do, so `require_scope` treats it as satisfying both. That rule lives in one
place in `auth`.

---

## 2. Scope step one and `scripts/setup_labels.py`

`SCOPES` becomes `[gmail.readonly, gmail.labels]`. Consent goes through the
existing `/auth/start` route. A test pins `SCOPES` to the exact list, so the
step 4 widening has to show up in a diff.

`setup_labels.py`:
- `require_scope(gmail.labels)` first
- `labels.list`, then create each missing name, then print a table: name, id,
  created or already present
- **Idempotent:** a second run creates nothing
- **Create-only.** It never renames, never deletes, and fails loudly on a
  case-variant clash (`agent/to action` already present) rather than guessing
- The names come from `categories`: `label_for(c)` for the six categories,
  plus `STOP_LABELS` and `NEEDS_REVIEW`. No second list is typed out. A test
  asserts the set has exactly nine members and every one starts with
  `LABEL_PREFIX`

**Record:** the consent timestamp, because it starts the 7-day clock this
phase measures.

## 3. Verifying the label queries against the real labels

Still no message writes: the human applies labels by hand in the Gmail UI.
`scripts/verify_labels.py` runs fixed queries and prints counts **against
expectations written into this document before it runs**:

- **Positive:** for each `Agent/` label with *k* hand-applied messages,
  `label_term(label)` returns exactly *k*. Must include the two multi-word
  labels, `To Action` and `Needs Review`, nested and spaced, which is the case
  `DESIGN.md` → Label naming could not test.
- **Complement:** over a fixed base query *B*,
  `count(B UNPROCESSED_TERMS) + count(B label:Processed) + count(B label:Error)
  == count(B)`. The test messages are labelled so the three states don't
  overlap. A wrong negation does not raise an error; it silently changes what
  the agent sees, and this is the identity that catches it.
- **The parent label:** whether `Agent/…` nests in the sidebar with no `Agent`
  label, and whether Gmail creates one. If one is needed, `setup_labels`
  gains a tenth name and this section is amended.

The hand-applied labels are removed by hand afterwards, and step 3 records
that they were.

## 4. Scope step two — `gmail.modify`

`SCOPES` becomes `[gmail.modify]`. Re-consent. Record the date, and whether
the step 2 token had already expired. `require_scope(gmail.modify)` guards
everything from here on.

## 5. `gmail_client.modify_labels` (A1)

The function in §1, its tests (one call, both sets, ids not names, empty
change raises, 403 not retried), and the guard's allowlist widened by exactly
one triple. Nothing calls it yet.

## 6. `scripts/undo_run.py` (A3)

### What the log has to carry first — `app/logbook.py`

Two schema changes, made in this step because undo is their first reader:

- `prior_labels: tuple[str, ...] | None`: the message's label **ids** at
  fetch time. Ids, not names, because a user label can be renamed and ids
  are what `modify` takes. Added now because it can't be recovered later: a
  field introduced afterwards is missing from every earlier row, including
  the large dry run.
- `Action.add` / `Action.remove` hold the **effective** delta: labels added
  that were not already present, labels removed that were. A remove of
  `INBOX` on a message that was never in the inbox is recorded as nothing.
  Names, as today, for readability. The intended (pre-intersection) action is
  derivable from `category` + the thresholds already in the row.

`Source` gains `"reply"` in step 7 with its call site, not here.

### The tool

```
uv run python scripts/undo_run.py --since 2026-10-30T10:00Z --until 2026-10-30T11:00Z
uv run python scripts/undo_run.py --since … --until … --apply
```

- `--since` and `--until` are **required, no default**, and compared against
  the log row's `timestamp`. `--until` may not be in the future, and
  `--since < --until`.
- **Prints the plan by default; writes only with `--apply`.** Undo is a write
  path too, and its first run against real mail should be read before it
  acts.
- Selects rows with `dry_run=False`, `error=None` and a non-empty action. For
  each message, **one** `modify_labels` call applying the inverse: remove what
  was added, add back what was removed.
- **An inverse delta, not an overwrite to `prior_labels`.** If the reader has
  changed the message since, for example starring it or adding their own
  label, those changes survive. Gmail has no "set labels to exactly X" call
  anyway.
- **Cross-check sweep:** `{label:"Agent/Processed" label:"Agent/Error"}`
  built from `STOP_LABELS`, with **no date restriction**. Any match with **no
  log row at all** is reported as an orphan, and never acted on
  automatically. Matches with a log row outside the window are out of scope
  and not reported.

  *Amended at plan review, 2026-11-02.* The sweep was first restricted to
  messages received since `--since`. That filters on *received* time while
  the window is *labelling* time, the same mismatch A3 fixes for undo
  itself. In practice it checked almost nothing: a live run at 10:00 over
  `newer_than:1d` labels mail received before 10:00, so a sweep of mail
  received after `--since 10:00` misses every message the run touched, and
  still reports "no orphans". The rehearsal has the same problem, because
  its messages are sent before their log rows are written. Dropping the
  filter costs one search and a set comparison against the log. The sweep
  only reports, so this does not widen what undo changes.
- Any other `Agent/` label found on a reverted message (a human re-file out
  of Needs Review) is stripped and reported.
- Writes its own report to `data/undo/<timestamp>.jsonl`, **not** to the
  classification log. Undo rows would distort every live metric, and the
  log's job is to record classifications.
- `--verify`: after `--apply`, re-fetch every touched message and assert its
  labels equal `prior_labels`, ignoring any the reader changed in between.
  This turns the gate's "the inbox is exactly as it was" check into a
  computed result instead of a visual check.

### The rehearsal

Five throwaway messages, sent to self. The human hand-applies `Agent/`
labels in a mix of states: a category plus `Processed` with `INBOX` removed,
`Needs Review` plus `Processed` kept, and one `Agent/Error` alone. Matching
log rows are written by a small fixture script, since nothing else writes
them yet. Then `undo_run.py --apply --verify`. **Pass:** all five back to
their prior labels, the `Agent/Error` one included, and the sweep reports no
orphans. This rehearsal passing is what unlocks step 7.

## 7. `app/agent.py` — `classify_and_label()`

```python
def classify_and_label(
    svc, message_id: str, *, cfg: Config, label_ids: Mapping[str, str],
    allowlist: Collection[str], run_id: str, run_kind: RunKind, dry_run: bool,
) -> logbook.Record:
```

Order, per message:

1. `fetch` (format=full, same as eval). Keep `label_ids` as `prior_labels`.
2. **Reply rule first:** `prefilter.is_reply(subject)` →
   `decide_reply_hit()`, `source="reply"`. Before the allowlist, so a `Re:`
   from an allowlisted promotional domain stays visible.
3. Allowlist → `decide_prefilter_hit()`, `source="prefilter"`.
4. Otherwise `classifier.classify(...)` → `decide(...)`, `source="model"`.
   A `ClassifierError` becomes `decide_failure()` with `error` set.
5. Effective delta = decision ∩/∖ `prior_labels`.
6. If not `dry_run` and the delta is non-empty: `modify_labels`. A Gmail
   error at this point becomes a failure row, and because the call is atomic
   nothing was applied.
7. `logbook.append`, **after** the modify.

`dry_run` is a parameter rather than read from config here, so the CLI's
`--live` is the only thing that can set it to `False` in this phase.
`NotAuthenticated` / `RefreshError` is not caught per message. It propagates
and halts the run (DESIGN.md → Error handling). Retry-with-delay on
`OllamaError`: up to **4 retries** after the first attempt (5 tries in total), inside the run
(delay to be set in the step 7 section). **No
cumulative failure counting.**

Tests: Gmail and Ollama mocked. One test per row of the decision table
reaching the right `modify_labels` call, or none in a dry run. Reply beats
allowlist. A failure makes no modify call and logs a row with `error`. Auth
failure halts. The effective delta on an already-archived message has no
`INBOX` remove.

## 8. The CLI — `scripts/run_pass.py`

```
uv run python scripts/run_pass.py --limit 50 [--query "newer_than:7d"] [--live]
```

- Base query is `-in:sent -in:drafts -in:chats` + `UNPROCESSED_TERMS`, plus
  the optional `--query`. The exclusions are not optional (DESIGN.md →
  Backfill).
- `--limit` is required. With `--live`, `--limit` must be at most
  `LIVE_LIMIT_CAP` (**25**), a module constant that Phase 4 removes
  deliberately.
- Dry run unless `--live`. `config.dry_run` is **not** consulted in this
  phase. The flag on the command line is the only way to write, so it is
  visible in shell history.
- Resolves label ids once and fails closed. Requires `gmail.modify` for
  `--live`, and only `gmail.readonly` for a dry run.
- `run_kind="manual"`. Prints one line per message (sender, subject,
  decision, action) and a summary at the end.
- Pacing: inference at ~2.6 s per message keeps fetches far below the
  measured ~150-fetch burst ceiling, so no extra `throttle` is needed. To be
  confirmed on the first 50.

## 9. The review tool — `scripts/review_run.py` (A4)

Agreed 2026-11-02 as written below, including that review output is not
committed. Made blind at the same review.

Reads one `run_id`'s rows from the log, fetches each message, and shows
sender, subject and the same truncated body the model saw. **The prediction
and intended action are not shown**, the same as eval labelling: the reviewer
cannot be anchored by them, so the figures are comparable with Phase 2's.
Predictions are joined to the reviewer's calls only when scoring, after the
review. The reader enters their own category with the existing digit keys. Reuses the keypress loop and labelling rules
from `app/evallabel.py` rather than copying them. Output goes to
`data/reviews/<run_id>.jsonl`, append-per-decision, resumable. It is not
committed: these messages are outside the eval set, and committing ids only
is a separate decision.

---

## Gate

Sample, draw and pass thresholds agreed 2026-11-02, before any dry run.

1. **Dry run, ~50 messages**, recent inbox. Read the log. Plumbing check: the
   intended actions look right, every row has `prior_labels`, the reply rule
   fires on `Re:` subjects.
2. **Large dry run, read as a second eval.**
   - *Population:* mail received **after 2026-09-11**, the eval frame's pinned
     `window_end`. It cannot overlap the eval set, and no tuning has seen it.
     About 700 messages at the frame's rate. *Size:* 300. *Draw:* the
     **most recent 300**. Accepted knowingly: it leans on recency, so the
     result describes current mail rather than the whole post-frame period.
   - *Review:* every message, blind, with step 9's tool.
   - *Pass:* action accuracy ≥ **0.84**, the lower end of dev's 0.900, and
     costly errors (a keeps-`INBOX` message archived) ≤ **2%** of reviewed
     keeps-`INBOX` truth.
   - *Reported, not gated:* six-way accuracy (comparable to dev's 0.771,
     since both were labelled blind, though on a different population);
     `To Action` retention; `Personal` →
     archive count; reply-rule precision.
   - *Watch triggers, checked here:* any `Personal` → archive starts the
     `Personal` prompt round from Phase 2, against a fresh holdout; that is
     scheduled as follow-up work, not done inside this gate. Reply-rule
     precision is recorded in `docs/BACKLOG.md`.
   - *Fail:* the classifier configuration is not tuned against these 300.
     Same lock-box logic as Phase 2's step 8.
3. **Live run over a tiny slice** — `--live --limit 12 --query newer_than:1d`.
   Inspect the inbox by eye.
4. **`undo_run.py --apply --verify`** over that window. Pass: `--verify`
   clean, no orphans.
5. Only then is bulk processing on the table, which is Phase 4.

---

## Build order

**Current step: 1** (not started).

1. Write interface + repo-wide guard + granted-scope check (A2). Code only,
   no consent, no API calls
2. Consent `readonly + labels` → `setup_labels.py` → run it
3. `verify_labels.py`, with the expectations written here first → run it
4. Consent `gmail.modify`; record token dates
5. `gmail_client.modify_labels` (A1)
6. `logbook` schema (`prior_labels`, effective delta) → `undo_run.py` →
   **rehearsal**
7. `agent.classify_and_label` + `Source "reply"`
8. `scripts/run_pass.py`
9. `scripts/review_run.py` (A4)
10. Gate

Per `CLAUDE.md`, each step is planned in its own section before it is
written, and nothing is committed by Claude.
