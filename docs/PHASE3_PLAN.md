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

**Current step: 7** (built 2026-11-04, 589 tests pass; procedure B, the cold-start check, passed 2026-11-04: 22.2 s, timeout stays 120 s). Step 6's rehearsal passed 2026-11-03, which unlocks `app/agent.py`.

1. Write interface + repo-wide guard + granted-scope check (A2). Code only,
   no consent, no API calls. **Done 2026-11-02.** Produced
   `tests/test_write_guard.py` (allowlist over `.users()` chains; replaces
   `test_gmail_client_contains_no_write_call`), `gmail_client.fetch_raw` /
   `label_ids` / `create_label` / `MissingLabels`,
   `auth.granted_scopes` / `require_scope` / `InsufficientScope` with the
   grant recorded in `token.json`, and `measure_bodies` routed through
   `fetch_raw`. 461 tests pass. The stored token has no grant record until
   step 2's consent, so `require_scope` fails closed until then
2. Consent `readonly + labels` → `setup_labels.py` → run it. **Done
   2026-11-03.** Consent at `2026-11-03T00:20:50Z`, which starts the 7-day
   clock. Granted, as stored: `gmail.labels` and `gmail.readonly`, offered by
   Google as two separate checkboxes. Step 1's grant capture works against a
   real consent. Preview showed nine to create and no clashes; `--apply`
   created all nine (`Label_2`..`Label_10`); a rerun showed nine present and
   nothing to create. No parent `Agent` label exists; the labels appear in
   the sidebar. Whether a scope change breaks an existing token was **not
   observed**: the old token had already expired, so re-consent was needed
   anyway. `require_scope` became `require_scopes` (see the Step 2 section).
3. `verify_labels.py`, with the expectations written here first → run it.
   **Done 2026-11-03.** Parent `Agent` label added to setup (the nine now
   nest). `--expect labelled` 13/13 and `--expect clean` 13/13 after
   removal. Label queries are verified against the real labels, which is
   the precondition `CLAUDE.md` sets for moving to `gmail.modify`.
   `label:"Agent"` does not match children's messages
4. Consent `gmail.modify`; record token dates. **Done 2026-11-03.**
   Consent at `2026-11-03T02:15:27Z`, which restarts the 7-day clock.
   Granted, as stored: `gmail.modify` alone, so grants did not accumulate.
   `require_scopes(MODIFY, LABELS, READONLY)` passes against it, and a
   `setup_labels` preview found all ten present, so `_IMPLIES` holds
   against a real grant. The step 2 token was alive (consented 00:20 that
   day). Whether a `SCOPES` change breaks an old token's refresh: **not
   observed again**, because consent came at 02:15, before the old access
   token's 02:19 expiry. Before re-consent, `status()` still correctly
   reported the old grant (`labels` + `readonly`), not the new request.
   Google's wording for the permission was not recorded. Three stale
   "read-only" claims were corrected (`main.py`, `label_eval.py`,
   `gmail_client._retryable`)
5. `gmail_client.modify_labels` (A1). **Done 2026-11-03.** One atomic
   `messages.modify`; both sides are required keywords; it returns the
   post-change label ids. It refuses an empty change, a bare string, the
   same id on both sides, and any system label but `INBOX`. The guard
   allowlist gains `("messages", "modify")`, and the new
   `MODIFY_LABELS_CALLERS` rule starts empty (A1 as a test). 487 tests
   pass. No live call made
6. `logbook` schema (`prior_labels`, effective delta) → `undo_run.py` →
   **rehearsal**. **Done 2026-11-03.**
   - Produced `Record.prior_labels` / `labels_after` and three validators,
     `decision.effective_change`, the pure `app/undo.py`, and the thin
     `scripts/undo_run.py`.
   - Also `scripts/rehearse_undo.py`, which acts on tagged throwaway mail
     only. Both scripts are on `MODIFY_LABELS_CALLERS`. `DESIGN.md` →
     Undo path amended per A3. 545 tests pass.
   - The plan was changed during build: `plan_message` became "earliest
     against now" with a hold on contradictory rows (see Q5).
   - The rehearsal, the first writes to real messages, passed first time on
     all three criteria: verify clean on all five, a rerun made zero calls,
     and the planted orphan was found and then cleared. A reader's star
     (`STARRED` + `YELLOW_STAR`) survived undo, and pre-archived M5 stayed
     archived
7. `agent.classify_and_label` + `Source "reply"`
8. `scripts/run_pass.py`
9. `scripts/review_run.py` (A4)
10. Gate

Per `CLAUDE.md`, each step is planned in its own section before it is
written, and nothing is committed by Claude.

---

## Step 1 implementation

**Status: agreed and built, 2026-11-02.** Code and tests only: no
consent, no API calls, `SCOPES` unchanged. Where this differs from §1, this
section is newer, and each difference is marked.

### What reading the code turned up

- **A write-verb denylist cannot run repo-wide.** §1 bans calls named
  `update`, `create`, `insert`, `send`, `delete` and so on across `app/` and
  `scripts/`. Plain Python uses the same names: `app/evalset.py:317` is
  `taken.update(picks)` on a set, and would fail the guard on day one.
  Excluding the test by receiver type isn't possible either, because the AST
  carries no types.
- **The granted scopes exist, then get thrown away.** After `fetch_token`,
  `google_auth_oauthlib` builds the credentials with
  `granted_scopes=session.token.get("scope")`, which is Google's answer. But
  `Credentials.to_json()` does not serialise `granted_scopes`, and
  `from_authorized_user_info(info, SCOPES)` sets `.scopes` to the requested
  list. So the stored token records only what was asked for. The token on
  disk today has no granted record at all.
- **`scripts/measure_bodies.py:219` bypasses `_execute`,** so it gets none of
  the backoff. Routing it through `gmail_client` fixes that too.
- **`scripts/spike_labels.py`** is Ollama-only and never touches Gmail. It
  needs nothing.

### The guard: an allowlist of API calls, not a denylist of verbs (changes §1)

Every Gmail API call goes through `svc.users()`. That is the one shape that
identifies an API call without knowing types, so the guard keys on it:

1. **Imports.** No file under `app/` or `scripts/` imports `googleapiclient`
   except `app/gmail_client.py` and the exempt file.
2. **No API calls outside `gmail_client`.** No file except
   `app/gmail_client.py` and the exempt file contains a call to `.users()`.
3. **`gmail_client`'s API calls are exactly an allowlist.** For each call
   chain rooted at `.users()`, record the names after it, such as
   `("messages", "get")`. The set found must **equal**:

   ```python
   ALLOWED_API_CALLS = {
       ("messages", "list"),   # search_refs
       ("messages", "get"),    # fetch_raw
       ("labels", "list"),     # label_ids          (new, step 1)
       ("labels", "create"),   # create_label       (new, step 1)
   }
   ```

   Step 5 adds `("messages", "modify")` in its own diff.

Why an allowlist:

- It fails closed. A write verb nobody thought to list (`batchModify`,
  `import_`, `settings().filters().create`) is still caught, because it is not
  on the list.
- New *reads* show up in a diff too, which costs nothing.
- `set.update` and similar are never `.users()`-rooted, so they are never seen.

A chain that does not resolve cleanly also fails. That includes binding a
resource to a variable (`m = svc.users().messages()` records
`("messages",)`, which is not on the list) and `getProfile` with no resource.
Limit, stated in the docstring: this catches mistakes, not deliberate evasion
(`getattr(svc, "users")`). Scope is the backstop for that.

**The exemption.** `EXEMPT = {"scripts/spike_gmail.py"}`, an exact path,
commented with why. A test asserts every exempt path still exists, so a
stale exemption can't linger after the file is gone. The script gains a
header comment: exempt from `tests/test_write_guard.py`, builds its own
client, must stay read-only.

`test_gmail_client_contains_no_write_call` is removed and the new module's
docstring says it replaces it with a strict superset.

### `app/gmail_client.py`

```python
def fetch_raw(svc, message_id: str) -> dict:
    """The full messages.get response, through _execute. fetch() is built on it."""

def label_ids(svc, names: Collection[str]) -> dict[str, str]:
    """name -> id for every requested label, by exact name.
    Raises MissingLabels naming *all* the missing ones, not just the first."""

def create_label(svc, name: str) -> str:
    """Create one user label; return its id."""
```

- `fetch` becomes `fetch_raw` plus parsing, with no behaviour change. This
  gives `measure_bodies` the raw payload it needs.
- `create_label` sends `{"name": name}` only and accepts Gmail's default
  visibility.
- `create_label` is not idempotent, but `_execute` still wraps it. If a
  create succeeds and the response is lost to a 5xx, the retry gets 409
  "already exists". 409 is not retryable, so it raises. Rerunning
  `setup_labels` (idempotent) then finds the label. A loud failure is
  correct here.
- `MissingLabels(GmailError)` is new.
- The module docstring changes: "read-only" becomes "one write so far,
  `labels.create`", pointing at the guard.

### `app/auth.py`

```python
READONLY = "https://www.googleapis.com/auth/gmail.readonly"
LABELS   = "https://www.googleapis.com/auth/gmail.labels"
MODIFY   = "https://www.googleapis.com/auth/gmail.modify"
SCOPES = [READONLY]                      # unchanged until step 2

_IMPLIES = {MODIFY: frozenset({LABELS, READONLY})}

class InsufficientScope(Exception): ...

def granted_scopes() -> frozenset[str]: ...
def require_scope(scope: str) -> None: ...
```

- **Storage.** `_write_token` saves `"granted_scopes"` at the top level of
  `token.json`, next to `consented_at` and for the same reason: it is a fact
  about the consent, not about the access token. On consent it comes from
  `creds.granted_scopes`, normalised from list or space-separated string to a
  sorted list. A refresh keeps the stored value, as it keeps `consented_at`.
- **No record means nothing granted.** `granted_scopes()` returns an empty
  set when the record is missing, which is the case for today's token. So
  `require_scope` fails closed until the next consent, which step 2 makes
  anyway.
- **Existing scripts are unaffected.** No existing read-only script calls
  `require_scope` in this step, so nothing breaks before re-consent. The
  first callers are `setup_labels` (step 2) and `run_pass` / `undo_run`.
- **`InsufficientScope` lives in `auth`,** not as a subclass of
  `gmail_client.NotAuthenticated`, because `gmail_client` imports `auth`.
  The other direction would be circular. Its message names the missing scope
  and says to reconnect via `/auth/start`.
- **`status()`** reports `"granted_scopes"` instead of `SCOPES`.
  `main.py`'s status line shows "not recorded, reconnect" when the set is
  empty.

### Tests

- `tests/test_write_guard.py`: the three rules above, run against the real
  tree. Then each rule against small synthetic sources, to prove the test can
  fail:
  - a `.users().messages().trash()` chain
  - an aliased resource
  - a `googleapiclient` import in a script
  - a `set.update` (must *not* fail)

  Plus: the exempt paths exist.
- `test_gmail_client.py`:
  - `fetch_raw` goes through `_execute`, and `fetch` still parses it
    unchanged
  - `label_ids` with all present, one missing, several missing (all named),
    and a case-variant name that does not match
  - `create_label` sends the name and returns the id
- `test_auth.py`:
  - consent stores the granted set, in both list and string form
  - refresh keeps it
  - a missing record gives an empty set
  - `require_scope`: an exact match passes; `MODIFY` satisfies `LABELS` and
    `READONLY`; `LABELS` does not satisfy `READONLY` or the reverse; an
    empty record raises, naming the scope
  - `status()` reports granted rather than requested: patch `SCOPES` to
    differ and check that the output follows the token

### Files touched

`app/gmail_client.py`, `app/auth.py`, `app/main.py` (one status line),
`scripts/measure_bodies.py`, `scripts/spike_gmail.py` (header comment only),
`tests/test_write_guard.py` (new), `tests/test_gmail_client.py`,
`tests/test_auth.py`. No new dependencies.

---

## Step 2 implementation

**Status: done 2026-11-03.** The first consent that
widens scope, and the first write to the mailbox: nine empty labels. No
message is touched, and `gmail.labels` cannot touch one.

### Code

**`app/categories.py`** gains the label set and the pure check over it. It
stays free of I/O, so it remains importable by `decision`.

```python
# Every label the agent owns. Built from the existing names, never retyped.
AGENT_LABELS: frozenset[str] = frozenset(
    {label_for(c) for c in Category} | {NEEDS_REVIEW, *STOP_LABELS}
)

@dataclass(frozen=True)
class LabelSetupPlan:
    present: tuple[str, ...]      # wanted, already exists by exact name
    to_create: tuple[str, ...]    # wanted, absent
    clashes: tuple[tuple[str, str], ...]   # (wanted, existing case variant)

def plan_label_setup(existing: Collection[str]) -> LabelSetupPlan: ...
```

- A clash is an existing name equal to a wanted one under `casefold()` but
  not exactly (`agent/to action` vs `Agent/To Action`). Gmail treats label
  names case-insensitively for uniqueness, so creating the wanted one would
  fail anyway. Reporting the clash beats a 409 halfway through.
- Labels outside `AGENT_LABELS` (`INBOX`, the reader's own) are ignored.

**`app/gmail_client.py`** gains `all_labels(svc) -> dict[str, str]`, with
`label_ids` rebuilt on it. This is the same `labels.list` call, so the
guard's allowlist does not change. `setup_labels` needs every name, not
only the wanted ones, to see case variants.

**`app/auth.py`:** `SCOPES = [READONLY, LABELS]`.

**`scripts/setup_labels.py`:**

```
uv run python scripts/setup_labels.py            # prints the plan, creates nothing
uv run python scripts/setup_labels.py --apply    # creates what is missing
```

1. `require_scopes(auth.LABELS, auth.READONLY)`. Readonly is not strictly
   needed for labels, but every later step assumes both, so a partial grant
   is caught here, the first time it can be.

   *Changed during step 2 (2026-11-03):* step 1's `require_scope(scope)`
   became `require_scopes(scope, *more)`. Entry points call it once, and the
   error names every missing scope at once, as `label_ids` does for labels.
   It takes separate arguments rather than a list, because a list parameter
   given one string would iterate its characters. Existing tests were
   amended; none were added. Everywhere above that says `require_scope`
   now means this.
2. `all_labels` → `plan_label_setup`.
3. Any clash: print it and exit non-zero **before creating anything**, even
   with `--apply`. The script never renames or deletes.
4. Without `--apply`, print the table (name, present / would create) and
   stop.
5. With `--apply`, `create_label` each missing name in sorted order. Then
   `label_ids(svc, AGENT_LABELS)` re-reads them, which proves all nine exist
   by the same lookup later steps use. Print name, id, created / already
   present.
6. Report whether a bare `Agent` label exists after creation. That is an
   observation for step 3, not a decision made here.

**Why `--apply`, when the plan said only "create-only":** it matches
`undo_run.py` and `run_pass.py`. Every script that can write shows its plan
first, and writing is a deliberate flag. Nine empty labels are easy to remove
by hand, so this is about consistency more than risk.

### Tests

- `test_categories.py`:
  - `AGENT_LABELS` has exactly nine members, all starting with
    `LABEL_PREFIX`, and it is exactly the six `label_for` values plus
    `NEEDS_REVIEW` and `STOP_LABELS`
  - `plan_label_setup`:
    - none present
    - some present
    - all present (nothing to create, which is what idempotence means)
    - a case variant counts as a clash, not as present or to-create
    - foreign labels are ignored
- `test_gmail_client.py`: `all_labels` returns every label, and `label_ids`
  behaves as before.
- `test_auth.py`: `SCOPES == [READONLY, LABELS]` exactly, so step 4's
  change has to appear in a diff.
- `setup_labels.py` itself stays thin and is not unit-tested. Its logic is
  `plan_label_setup` plus three `gmail_client` calls, each already tested.

### Expected side effect

Changing `SCOPES` probably breaks the current token before re-consent. A
refresh sends the requested scopes, and Google is expected to refuse a
refresh that asks for more than was granted. The token then reads as
`needs_auth`. So the code change and the consent should happen in one
sitting. Whatever happens is recorded, as it is the first real evidence of
how the token behaves across a scope change.

### The manual procedure (the human)

1. Land the code; tests pass.
2. Start the server (`uv run uvicorn app.main:app --port 8000`) and visit
   `/auth/start`. Google may show separate checkboxes per scope: **tick
   both.** If Google refuses `gmail.labels` outright, add it under the
   consent screen's Data access settings in the Cloud console and retry.
3. **Check the step 1 assumption against a real consent:**
   - `data/token.json` has `granted_scopes` listing `gmail.labels` and
     `gmail.readonly`
   - the status page at `/` shows both
   - If `granted_scopes` is missing, stop. Step 1's capture does not work
     against real Google, and `setup_labels` will refuse to run.
4. `uv run python scripts/setup_labels.py`: read the plan. Expected: nine to
   create, no clashes.
5. `uv run python scripts/setup_labels.py --apply`: nine created, nine ids.
6. Run it again with `--apply`. Expected: nine present, nothing created.
7. Look at the Gmail sidebar and note how the labels display. That is
   input to step 3.

### Recorded when done

- The consent timestamp (`consented_at`), which starts the 7-day clock.
- The granted set exactly as stored.
- Whether the token broke on the `SCOPES` change, before re-consent.
- Whether an `Agent` parent label exists, and how the sidebar shows the
  nine.

---

## Step 3 implementation

**Status: done 2026-11-03.** Still no message writes by
code. The human labels a handful of messages by hand. A read-only script
checks that our queries find exactly those messages, then checks again,
after the labels are removed, that the queries find nothing.

### Part 1: the parent label (settles §3's third bullet)

Observed in step 2: with no `Agent` label, the nine show flat. Gmail nesting
is naming only: `Agent/Bookings` nests under `Agent` if and only if a label
named exactly `Agent` exists. **Decision (2026-11-03): create it.**

- `categories.LABEL_PARENT = "Agent"`, derived from `LABEL_PREFIX`. It moves
  out of `setup_labels.py`.
- `categories.SETUP_LABELS = AGENT_LABELS | {LABEL_PARENT}`, ten names.
  `plan_label_setup` works over `SETUP_LABELS`.
- `AGENT_LABELS` stays the **nine**: the labels the agent applies to messages.
  The parent is never put on a message, so it is not in `AGENT_LABELS`,
  and so not in `label_ids` lookups or the undo sweep.
- `setup_labels.py` drops its "parent exists?" report; the parent is just a
  tenth row now. Rerunning `--apply` creates only `Agent`.
- Tests: the existing `plan_label_setup` tests are amended from nine to ten.
  The "nine in `AGENT_LABELS`" test stays as it is.

The parent is created **before** the query checks run, so the queries are
verified in their final setup.

### Part 2: `scripts/verify_labels.py`

```
uv run python scripts/verify_labels.py --expect labelled
uv run python scripts/verify_labels.py --expect clean
```

Read-only: `require_scopes(auth.READONLY)`, and only `gmail_client.count`.
Every query is built with `label_term` and `UNPROCESSED_TERMS`, the same
helpers the agent will use. That is the point: we test the real query
builders, not hand-typed strings.

**The base query `B`** is `-in:sent -in:drafts -in:chats`, the exclusions
`run_pass` will use. `count(B)` is taken first and again last. If it changed,
mail arrived mid-check and the run is reported void, not failed.

**Checks:**

1. **Positive:** `count(B label_term(L))` for every label `L` in
   `AGENT_LABELS`, against the expected count below.
2. **Negation:** `count(B label_term(L, negate=True)) == count(B) − k` for the
   two multi-word labels, `To Action` and `Needs Review`. These are the
   names most likely to be mis-parsed.
3. **The complement identity:**
   `count(B UNPROCESSED_TERMS) + count(B Processed) + count(B Error) == count(B)`.
   A wrong negation raises no error; it just quietly changes what the queue
   holds. This identity is what catches it.
4. **Reported, not checked:** `count(B label:"Agent")`, so we learn whether
   searching the parent also returns its children's messages. Nothing depends
   on it, but it is worth knowing before Phase 4.

Output: one row per check (query, expected, actual, PASS or FAIL); exit
non-zero on any FAIL.

**Expectations, fixed here before running.** Six received messages, called
M1 to M6:

| Message | Labels applied by hand |
|---|---|
| M1 | `To Action`, `Processed` |
| M2 | `To Action`, `Processed` |
| M3 | `Needs Review`, `Processed` |
| M4 | `Receipts`, `Bookings`, `Personal`, `Promotions`, `Updates`, `Processed` |
| M5 | `Error` |
| M6 | none (the control) |

`--expect labelled`:
- `To Action` 2; `Needs Review` 1; `Receipts`, `Bookings`, `Personal`,
  `Promotions`, `Updates` 1 each
- `Processed` 4; `Error` 1; unprocessed = `count(B)` − 5

M4 carrying five categories is not something the agent would ever do. It is
there so every label name is searched at least once using only six messages.

`--expect clean`: every label count is 0, and unprocessed equals
`count(B)`. Run after the labels are removed. It proves the removal and
gives the queries a second, independent check.

Both sets of numbers live in the script as constants, matching this table.

**Testing:** the script is not unit-tested, which is the same call as
`setup_labels`. Its expectations come from a human count, not from the code,
so a bug in the checker most likely shows up as a FAIL rather than a false
PASS. The `clean` run is the second check.

### The manual procedure (the human)

A. Land the code; tests pass.

B. `uv run python scripts/setup_labels.py --apply`. Expected: `Agent`
   created, nine present. Check that the sidebar now nests the nine.

C. **Turn conversation view off** (Gmail settings, General tab).
   - With it on, labelling one message labels its whole thread, and the
     counts would be wrong for a reason unrelated to the queries.
   - Pick six received messages, not ones you sent.
   - Apply the labels exactly as in the table. M6 gets nothing; it only
     needs to exist.

D. `uv run python scripts/verify_labels.py --expect labelled`. All PASS.

E. Remove every hand-applied label from M1 to M5.

F. `uv run python scripts/verify_labels.py --expect clean`. All PASS.

G. Turn conversation view back on if you want it.

### Recorded when done

- **Baseline, before anything was labelled or the parent created
  (2026-11-03):** `--expect clean` gave 13/13 PASS, base = 19,823 messages,
  about 1m48s per run. Weak evidence on its own: Gmail returned 0 for
  `label:"Agent"`, which did not exist yet, without error. A misparsed
  query returns 0 silently, so only the `labelled` run can tell a correct
  query from a broken one.
- **Parent created (procedure B):** `Agent` exists alongside the nine,
  confirmed by `all_labels` on 2026-11-03.
- **`--expect labelled` (procedure D), 2026-11-03: 13/13 PASS**, base
  19,823 throughout. Every label name, including the nested multi-word
  `Agent/To Action` and `Agent/Needs Review`, matched exactly its
  hand-counted messages. Both multi-word negations and the complement
  identity held (19,818 unprocessed + 4 + 1 = 19,823). This is the case
  `DESIGN.md` → Label naming could not test, now tested.
- **Parent search does not include children:** `label:"Agent"` matched 0
  messages while its sub-labels sat on 5. Each label matches only messages
  carrying it directly.
- **`--expect clean` after removal (procedure F), 2026-11-03: 13/13 PASS**,
  as reported by the human (output not pasted). The hand-applied labels on
  M1-M5 are removed.
- Whether `label:"Agent"` matches child-labelled messages.
- That the hand-applied labels were removed, which is shown by the `clean`
  run.

---

## Step 4 implementation

**Status: done 2026-11-03.** The token gains the right
to change messages. **No code gains the ability to:** until step 5 the
write guard's allowlist has no `messages.modify`, so from this step until
that one the token can write but no code path can. This is the reverse of
Phases 0 to 2, where code might have tried but the token would have
refused.

### Code

**`app/auth.py`:** `SCOPES = [MODIFY]`. `gmail.modify` includes everything
`gmail.labels` and `gmail.readonly` allow (`_IMPLIES`, step 1), so asking for
all three would add nothing. Requesting one scope keeps the consent screen
honest about what is being granted. The module docstring and the `SCOPES`
comment say step 4 has happened.

**Callers need no change.** `setup_labels` asks for `LABELS, READONLY` and
`verify_labels` asks for `READONLY`. A grant of `MODIFY` covers both through
`_IMPLIES`. The existing `require_scopes` test table already has that row.

**Stale "read-only" claims, corrected** because they are now false:

- `app/main.py:46`: the page still says "Phase 0 - read-only". It becomes
  "Phase 3", with the granted scopes already shown beside it.
- `scripts/label_eval.py:25`: "the token carries `gmail.readonly` besides".
  The script is still read-only by construction (lists and gets only, and
  the guard enforces that). Only the claim about the token goes.
- `app/gmail_client.py:204`, the `_retryable` docstring: "what the
  read-only scope produces if a write ever slips in" becomes "what an
  insufficient scope produces". The behaviour is unchanged: a permission
  403 is never retried.

### Tests

Amended only, none added:

- `test_scopes_are_pinned`: `SCOPES == [auth.MODIFY]`. Its docstring now
  says this is the last widening in Phase 3.

Nothing else depends on the requested list. `test_status_reports_granted_not_requested`
patches `SCOPES` itself.

### Expected side effects

- **Grants may accumulate.** Google keeps every scope a user has granted to
  this app. The new token's `granted_scopes` may list all three or only
  `gmail.modify`. Either passes; the record says which.
- **The old token may stop refreshing** once `SCOPES` changes, as predicted
  and not observed in step 2. It only shows when the current access token
  expires (hourly), so it is recorded only if seen.
- **The 7-day clock restarts** at this consent.

### The manual procedure (the human)

A. Land the code; tests pass.

B. Record whether the step 2 token was still alive. `consented_at` was
   `2026-11-03T00:20:50Z`, so it should be, with about 6 days left.

C. Start the server, visit `/auth/start`, and consent. **Read Google's
   description of the permission, and record its wording.** It is the first consent that lets the app
   change mail. If Google refuses the scope, add `gmail.modify` under the
   consent screen's Data access settings in the Cloud console.

D. Check the grant:
   - `data/token.json` → `granted_scopes` includes `gmail.modify`
   - the status page shows it

E. Run `uv run python scripts/setup_labels.py` (preview only). Expected: all
   ten present, nothing to create. This shows a real `gmail.modify` grant
   satisfies a `LABELS + READONLY` requirement through `_IMPLIES`, and that
   label reads still work under the new token.

### Recorded when done

- `consented_at` (the new 7-day clock).
- `granted_scopes`, exactly as stored.
- Whether the step 2 token had expired, and whether the old token was seen
  to break.

---

## Step 5 implementation

**Status: done 2026-11-03.** The first function that
can change a message. Code and tests only: **nothing calls it**, and no
message is touched in this step. Its first real use is step 6's
rehearsal, on throwaway mail.

### `gmail_client.modify_labels`

```python
def modify_labels(
    svc,
    message_id: str,
    *,
    add_ids: Collection[str],
    remove_ids: Collection[str],
) -> tuple[str, ...]:
    """The single atomic label change. Returns the message's label ids
    after the change, as Gmail reports them."""
```

One `messages.modify` call carrying `addLabelIds` and `removeLabelIds`
together, through `_execute`. Both keywords are **required**, even when
one side is empty, so every call site states both halves of the change.

**Checks before any API call.** Each is a caller bug, so it raises rather
than silently doing less:

1. **Empty change**: both sides empty → `ValueError` (from §1).
2. **A string instead of a collection** → `TypeError`. `add_ids="Label_3"`
   would otherwise be read as the characters `L`, `a`, `b`... It is the
   same trap `require_scopes` avoids with separate arguments. Here a
   keyword list is clearer, so the check is explicit instead.
3. **The same id on both sides** → `ValueError`. Gmail's behaviour then is
   undocumented, and no correct caller asks for it.
4. **Only `INBOX` and user labels may be touched.** Every id must be
   `INBOX` or start with `Label_`, the form Gmail gives user labels
   (observed in step 2: `Label_2` to `Label_10`). Anything else raises:
   `TRASH`, `SPAM`, `UNREAD`, `STARRED`, `IMPORTANT`, `CATEGORY_*`.
   - The agent and undo only ever move `INBOX` and `Agent/` labels, so
     this costs nothing. In return, `modify_labels` **cannot** trash,
     spam, un-spam or mark read a message, whatever a caller passes.
   - If Google ever changed the id format, this fails closed: it raises,
     and does not let something through.

Ids are sorted and de-duplicated before sending, so the request is
deterministic and testable.

**Ids, not names.** Both are strings, so the type cannot enforce it. Ids
come from `label_ids()`. A name passed by mistake (`Agent/Receipts`) fails
check 4, because it neither is `INBOX` nor starts with `Label_`. So the
system-label rule also catches the ids-versus-names mistake.

**Retries.** `messages.modify` sets labels and does not toggle them, so a
retried call that already succeeded changes nothing. That makes
`_execute`'s backoff safe here. A permission 403 is still never retried.

**Why return the resulting labels** (§1 had `-> None`): Gmail's response
already carries them, so it costs nothing. Undo's `--verify` and the
agent's log can then record the state Gmail confirmed, not the state that
was asked for.

### The guard

- `ALLOWED_API_CALLS` gains `("messages", "modify")`, the change §1 said
  would come in this step's own diff.
- **A1 as a test.** New in `test_write_guard.py`: no file under `app/` or
  `scripts/` other than `gmail_client.py` refers to `modify_labels`, except
  those in `MODIFY_LABELS_CALLERS`. It starts **empty**.
  - Step 6 adds `scripts/undo_run.py`, and the rehearsal script that
    writes fixture rows if it needs it.
  - Step 7 adds `app/agent.py`.
  - So "undo is the only caller until the rehearsal passes" is enforced,
    not just a convention.
  - A "refers to" scan catches `gmail_client.modify_labels(...)`,
    `from app.gmail_client import modify_labels`, and passing the function
    around by name.

### Tests

In `test_gmail_client.py`, with `FakeMessages` gaining `modify`:

- one call, with the body holding both `addLabelIds` and `removeLabelIds`,
  sorted and de-duplicated
- an empty change raises, and no call is made
- a string on either side raises `TypeError`, and no call is made
- the same id on both sides raises
- `TRASH`, `SPAM`, `UNREAD` and a label *name* on either side raise;
  `INBOX` and `Label_7` are accepted
- returns `labelIds` from the response
- a permission 403 from `modify` is raised after one attempt; a 429 is
  retried

In `test_write_guard.py`:

- the allowlist equality test picks up the new pair
- the callers test, plus a synthetic case showing a reference from another
  file fails it

### Not in this step

- No live call. A modify on a real message would be the first write to
  mail, and that belongs to step 6's rehearsal, where undo exists to
  reverse it.
- No log schema change. That is step 6.

---

## Step 6 implementation

**Status: done 2026-11-03.** All code built (545 tests pass), `DESIGN.md` amended per A3, and the rehearsal passed. See Recorded when done. A plan-only `undo_run.py` against the real mailbox resolved all ten ids (`INBOX` by name) and swept no orphans. All eleven design questions were
settled at review and are recorded in [the decisions table](#decisions-taken-step-6-review-2026-11-03)
at the end of this section. Q references below point there.

The first step that changes real messages. Every write is to throwaway mail
sent to self, and undo exists before the first one. Three parts, in this
order: the log schema, the undo tool, the rehearsal.

### What reading the code turned up

- **No log rows exist.** `data/classifications.jsonl` is absent, and nothing
  in `app/` or `scripts/` constructs a `logbook.Record` yet. Eval rows go
  elsewhere. So the schema change needs no migration, and new fields can be
  validated strictly from the first row.
- **The vocabularies differ.** `prior_labels` is ids (§6). `Action` is names
  (§6), which is what `decision` produces. `modify_labels` takes ids. So any
  code that compares the two needs the `name -> id` map from `label_ids()`.
  That map is the only bridge, and a missing `Agent/` label fails closed
  there.
- **Phase 3's agent rows have a fixed shape.** Every `decide_*` function adds
  only `Agent/` labels and removes only `INBOX`. Nothing removes an `Agent/`
  label until Needs Review reconciliation in Phase 4. Undo can therefore
  check this shape and refuse any row that breaks it (Q5).
- **§6's rehearsal has an ordering flaw.** The human hand-applies labels,
  and then a fixture script writes the rows. But `prior_labels` means the
  labels *before* the write, and by the time the fixture runs, the labels it
  would fetch are the ones *after*. The fixture would have to be told the
  prior state, and it would then be testing a hand-typed input rather than
  the fetch-then-write order step 7 will use. See Q1.
- **The sweep needs a precise "has a log row".** §6 says any match "with no
  log row at all" is an orphan. A dry-run row, or a failure row, does not
  explain a label on a message. So an orphan is defined below as a message
  with no *applied* row.

### Part 1: the log schema — `app/logbook.py`

```python
class Record(BaseModel):
    ...
    # Label ids at fetch time, before any write. None only when the fetch
    # itself failed. Ids, not names: user labels can be renamed, and ids are
    # what modify takes.
    prior_labels: tuple[str, ...] | None = None
    # Label ids Gmail reported after the modify (modify_labels' return).
    # None when no modify was made: a dry run, a failure, or an empty delta.
    labels_after: tuple[str, ...] | None = None          # Q3
    action: Action = ...   # now the EFFECTIVE change, names, as §6
```

**Validators** (Q10). `Record` is the class that defines one log row. A
validator is a check pydantic runs whenever a `Record` is created; if it
fails, creating the row raises, so a bad row can never be written. Writes are
strict, so three rules become things a row physically cannot break:

1. **A row with a label change must carry what the labels were before.** A
   non-empty `action` requires `prior_labels`. Undo cannot trust a change it
   cannot compare against anything.
2. **A failed row carries no label change.** `error` set requires an empty
   `action`. This is `CLAUDE.md`'s "a failure applies no labels".
3. **A dry run has no "after".** `labels_after` set requires `dry_run=False`.

`Source` is unchanged. `"reply"` arrives in step 7, as §6 says.

**The effective change lives in `app/decision.py`, not `logbook`** (Q7).
`logbook` only writes and reads rows and would never call it. The question
"given this decision and this message's current labels, what actually
changes?" is the last step of deciding, so it belongs with the decision
table:

```python
def effective_change(
    decision: Decision, prior_names: Collection[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(add, remove): the decision's adds not already present, and its
    removes that were. Pure; names only."""
```

- **Names only**, so label ids stay out of `decision`, which today speaks
  only names. The caller turns the message's `prior_labels` ids into names
  with the inverted `label_ids()` map. Ids not in the map (system labels,
  the reader's own labels) are dropped, because only `Agent/` labels and
  `INBOX` can appear in a decision.
- **It returns tuples, not a `logbook.Action`.** `test_decision.py` allows
  `decision` to import only `app.categories` and the standard library, and
  that does not change. The caller builds the `Action`.
- Its callers are the rehearsal fixture (this step) and `agent.py` (step 7).
  The fixture cannot import from `agent.py`, which must not exist yet, so
  the function has to live somewhere both can reach.

### Part 2: undo — `app/undo.py` (pure) and `scripts/undo_run.py` (thin)

The same split as `categories.plan_label_setup` and `setup_labels.py`. All
the decisions live in a module with no I/O, so they can be tested without a
mock. The script only parses arguments, makes Gmail calls and prints.
`app/undo.py` never refers to `modify_labels`, so the guard's caller list
stays at the scripts.

**What undo is for.** Reversing *a run*, not a single message. A single
misfiled message is fixed by hand in Gmail; that is a correction, and Phase
4's reconciliation is where corrections are handled. Undo is for "that run
was wrong", and the gate uses it to reverse the 12-message live run as a
check. So it always works over a time window, matched on **when the agent
changed the label** (the log row's timestamp), not when the email arrived.
"Undo 14:00–14:30" means everything the agent did in that half hour. No
single-message option (Q11).

**What the parts do, in plain terms.** Take one message the agent filed as
Receipts at 10:00, adding `Receipts` and `Processed` and removing `INBOX`:

- **`parse_window`** checks the two times typed on the command line before
  anything else happens. It changes nothing.
- **`plan_message`** decides what to do to the message. It reads the log
  row, looks at the labels the message has *now*, and works out the opposite
  change: remove `Receipts` and `Processed`, put `INBOX` back. This is
  printed before anything is applied.
- **`orphans`** looks for messages the log does not explain. It searches the
  whole mailbox for `Agent/Processed` or `Agent/Error` and checks each match
  against the log. A labelled message with no row is an orphan: the program
  crashed between the label change and writing the row, so undo would never
  know to touch it. Orphans are reported, never fixed, because with no row
  there is no record of what the message looked like before.
- **`verify`** checks that undo worked. After undo, it re-fetches each
  message and compares it with `prior_labels`, allowing for the reader's own
  changes in between (a star stays). Any difference is reported, and the
  exit is non-zero. This replaces looking at the inbox and judging that it
  looks right.

The plan step decides, the orphan check catches what was missed, and verify
proves the result.

```python
@dataclass(frozen=True)
class Window:
    since: dt.datetime   # inclusive
    until: dt.datetime   # exclusive

def parse_window(since: str, until: str, *, now: dt.datetime) -> Window:
    """ISO 8601, timezone REQUIRED (a naive time is refused, not assumed UTC).
    since < until, until <= now, until - since <= MAX_WINDOW."""     # Q6

def applied(record: Record) -> bool:
    """dry_run=False, error=None, non-empty action. The only rows undo reads."""

def select(records: Iterable[Record], window: Window) -> dict[str, list[Record]]:
    """Applied rows whose timestamp is in the window, grouped by message.
    Raises on any row whose shape is not agent-shaped (adds ⊆ AGENT_LABELS,
    removes ⊆ {INBOX})."""                                            # Q5

@dataclass(frozen=True)
class MessagePlan:
    message_id: str
    add_ids: tuple[str, ...]       # what to put back (INBOX at most)
    remove_ids: tuple[str, ...]    # what to take off
    stray: tuple[str, ...]         # Agent/ labels not added by any row, being stripped
    already_reverted: bool         # nothing to do; no modify call is made

def plan_message(
    rows: Sequence[Record], current_ids: Collection[str],
    ids_by_name: Mapping[str, str],
) -> MessagePlan:
    """The inverse of the rows, intersected with the CURRENT labels."""  # Q4

def orphans(swept_ids: Collection[str], records: Iterable[Record]) -> set[str]:
    """Swept messages with no applied row anywhere in the log, in or out of
    the window."""

def verify(
    rows: Sequence[Record], before_undo: Collection[str],
    after_undo: Collection[str], ids_by_name: Mapping[str, str],
) -> list[str]:
    """Mismatches, empty when clean. See the rule below."""

class UndoReport(BaseModel): ...   # one row per message, data/undo/<stamp>.jsonl
```

**Planning a message** (Q4, Q5). The aim is the message as it was before
the agent first touched it. Two rules:

- **`Agent/` labels, earliest against now:** `remove_ids` = every `Agent/`
  label present now that was not in the earliest row's `prior_labels`. That
  covers everything the agent added and anything the reader re-filed into
  since. The second kind is reported as `stray` (§6's re-file out of Needs
  Review).
- **`INBOX`, only if the agent archived it:** `add_ids` = `INBOX`, if any
  row removed it **and it is absent now**. Restoring `INBOX` to its
  earliest state instead would override the reader. It would un-archive a
  message they archived themselves (M4's case if they had), or re-archive
  one they moved in.
- **The cross-check, `held`:** the `Agent/` rule trusts `prior_labels`
  alone, so a second record checks it. A row's adds are the effective
  change, so no row should ever add a label already in the earliest
  `prior_labels`. If one does, either `prior_labels` was recorded wrong or
  the reader removed a pre-existing `Agent/` label that the agent then
  re-added. The message is then **held**: untouched, reported, and the exit
  is non-zero. Without this check, a wrong `prior_labels` would fool undo
  and `verify` together, since both trust it.

If both sets are empty, the message is `already_reverted` and no call is
made (`modify_labels` would refuse an empty change anyway). Otherwise there
is one `modify_labels` call per message, whatever the number of rows.

**Why compare with the current labels (Q4), stated honestly:** it is about
honest reporting, not safety. Gmail ignores removing a label that is absent
and adding one that is present, so replaying the logged inverse blindly
would leave the mailbox in the same state in nearly every case. What
differs is what undo says, and whether it makes calls. Take M1, archived at
10:00. The reader moves it back to the inbox and removes `Receipts` at
10:20, then runs undo at 10:30:

| | Blind replay of the log | Against current labels |
|---|---|---|
| Sends | remove Receipts, Processed; add INBOX | remove Processed |
| Reports | "removed Receipts, restored INBOX" (neither happened) | "Receipts already gone, already in inbox, removed Processed" |
| Second run | 5 calls, each reported as a change | "already reverted", 0 calls |
| Extra cost | — | none: `stray` and `verify` already fetch every message |

Pass criterion 2 (zero modify calls on a rerun) is only checkable this way.

**Several rows for one message (Q5).** This happens when one window covers
"labelled, undone, labelled again". The "removed" step never appears in the
log, because the agent never removes an `Agent/` label or adds `INBOX`.
Undo and the reader write no classification rows, and `select` refuses
any row that is not agent-shaped. So every row is the same kind of change,
and repeats add nothing new: only the earliest `prior_labels` and "did any
row archive it" matter.

*Changed during build, 2026-11-03:* first written as a union of every row's
inverse. That was replaced by the rules above, which read more plainly.
Checked over 400,000 random agent-shaped histories, with the reader and
earlier undos acting in between. Where no `Agent/` label existed before
the agent, the two agreed every time. Where one did, they differed only
when a row re-added a label already in the earliest `prior_labels`. There
the union would have removed a label the message had before, which
`verify` then reports as missing. The rules above hold those messages
instead.

**The verify rule.** "Equal to `prior_labels`, ignoring any the reader
changed" (§6), made computable. Let `P` be the earliest row's
`prior_labels`, `B` the labels just before undo, `A` the labels after it,
and `L` the latest row's `labels_after`:

- **`Agent/` labels:** `A ∩ agent_ids == P ∩ agent_ids`. Undo owns these
  outright, so a reader change is no excuse.
- **`INBOX`:** if the reader moved the message since the write
  (`INBOX ∈ B` differs from `INBOX ∈ L`), `A` keeps the reader's choice.
  Otherwise `INBOX ∈ A` iff `INBOX ∈ P`. Without `labels_after`, the rule
  falls back to `P`.
- **Everything else** (`STARRED`, `UNREAD`, the reader's own labels):
  `A` equals `B` there. Undo sent no such ids, and `modify_labels` refuses
  system labels, so this should hold by construction. It is checked anyway,
  because it is the claim "an inverse, not an overwrite" makes.

**`scripts/undo_run.py`:**

```
uv run python scripts/undo_run.py --since … --until …                 # plan only
uv run python scripts/undo_run.py --since … --until … --apply [--verify]
                                 [--log data/rehearsal/classifications.jsonl]   # Q2
```

1. `parse_window`. Any failure exits before any Gmail call.
2. `require_scopes(READONLY)`, or `MODIFY` with `--apply`.
3. `label_ids(svc, AGENT_LABELS)` plus `INBOX`. Fails closed.
4. `read_all(log)`. If any lines were skipped, print the count, and refuse
   `--apply`: a skipped line could be a row that should have been undone.
5. `select`. A row that breaks the shape check stops the run here.
6. **Sweep:** `search_ids` over `{label:"Agent/Processed" label:"Agent/Error"}`,
   built from `STOP_LABELS` with `label_term`, with no date term. Then
   `orphans`. These are reported only, never acted on.
7. For each selected message, `fetch` its current labels, then
   `plan_message`, then print it. Without `--apply`, stop after the table and
   the orphan list.
8. With `--apply`, one `modify_labels` per message that is not already
   reverted. Append its `UndoReport` row after each call, so a crash
   mid-run leaves a true record of what was done. A Gmail error on one
   message is reported, and the run continues with the next.
9. `--verify`: re-fetch every selected message and run `verify`. Any
   mismatch exits non-zero.

Exit codes: 0 means clean. Non-zero on any error, any verify mismatch, or
any orphan, because an orphan is the thing the sweep exists to find.

### Part 3: the rehearsal — `scripts/rehearse_undo.py`

**Changes §6** (Q1): the labels are applied by the script through
`modify_labels`, not by hand. Each fixture message goes through the same
order step 7 will use: `fetch` (keep `prior_labels`), take a decision from
the real `decide_*` functions, `decision.effective_change`, `modify_labels`, then
`logbook.append` after the modify. So the rehearsal tests the write half as
well as the undo half, and `prior_labels` is a fetched fact rather than a
typed one.

**It cannot touch real mail.** It finds its messages with
`search_ids(svc, 'subject:"[personai rehearsal]"')` and acts only on
messages whose subject is exactly `[personai rehearsal] M<n>` for a known
`n`. It refuses to run if any tag is missing or duplicated, or if the search
finds any message without a tag. The tag is the only way in, so there is no
`--ids` or `--query` argument (Q9). Plan by default; `--apply` writes. Rows
go to `data/rehearsal/classifications.jsonl` (Q2), with `run_kind="manual"`
and `note="rehearsal fixture"`. At the end it prints the exact `undo_run.py`
command for the window it just wrote.

**Built 2026-11-03, with checks the plan did not spell out.** Every check
runs before the first write, so a refusal changes nothing:
- **Start state:** M1–M4 must be in the inbox, and M5 archived. None of
  M1–M6 may carry an `Agent/` label yet, so M6 is labelled only after the
  script runs (procedure D).
- **The config:** each decision is checked against the fixture table below,
  using the real `config.get()` thresholds. A changed config is refused
  rather than allowed to quietly change the rehearsal. Checked offline:
  all five match at `T=0.8`, `F=0.15`.
- **On failure:** it stops at the first `modify_labels` failure. The call is
  atomic, so that message is untouched, and undo can revert every row
  already written.

**The fixture, fixed here before it runs (Q8).** Six messages sent to self:

| Msg | Before the fixture (human) | Scripted decision | Effective action | Between fixture and undo (human) |
|---|---|---|---|---|
| M1 | — | `decide()`, Receipts 0.95 | +Receipts +Processed −INBOX | **star it** |
| M2 | — | `decide()`, max 0.55 → Needs Review | +Needs Review +Processed | **re-file**: remove Needs Review, add To Action |
| M3 | — | `decide_dead_letter()` | +Error | — |
| M4 | — | `decide()`, To Action 0.95 | +To Action +Processed | — |
| M5 | **archive it** | `decide()`, Receipts 0.95 | +Receipts +Processed, **no INBOX remove** | — |
| M6 | — | none (not in the script) | — | **add `Agent/Processed` by hand** (the planted orphan) |

What each one tests:

- **M1:** the archive and its restore, and that a reader's star survives
  undo.
- **M2:** the inverse against *current* labels (Needs Review is already
  gone), and the `stray` strip of a re-file.
- **M3:** `Agent/Error` alone, the branch an undo that sweeps only
  `Processed` would miss.
- **M4:** the keeps-`INBOX` case, so undo must not touch `INBOX`.
- **M5:** the case A3 exists for. Undo must **not** put an archived message
  back in the inbox.
- **M6:** the sweep can fail. A check that can only ever report "no
  orphans" proves nothing.

Synthetic distributions for `decide()` are passed in the script, with the
shipped `T=0.8` and `F=0.15`. They exercise the decision table, not the
model, and they sit in a separate log, so they never reach a metric.

**Expectations, fixed here before running.**

Plan-only `undo_run` over the fixture window:

| Msg | remove | add back | Note |
|---|---|---|---|
| M1 | Receipts, Processed | INBOX | |
| M2 | Processed, To Action | — | To Action listed as `stray` |
| M3 | Error | — | |
| M4 | To Action, Processed | — | |
| M5 | Receipts, Processed | — | **no INBOX** |

Orphans: exactly `{M6}`.

After `--apply --verify`:
- verify is clean on all five
- M1 is starred and in the inbox
- M5 is archived
- M6 still carries `Agent/Processed`
- exit is non-zero, because of the orphan

**Pass**, which unlocks step 7:
1. Both tables above match exactly.
2. A second `--apply --verify` over the same window reports all five as
   already reverted, and makes zero modify calls.
3. Once the human removes M6's label, a plan-only run reports zero orphans
   and exits 0.

### The guard

`MODIFY_LABELS_CALLERS = {"scripts/undo_run.py", "scripts/rehearse_undo.py"}`,
each with a comment saying why. `app/agent.py` stays off it until this
step's Recorded section says the rehearsal passed.

### Tests

- `test_logbook.py`:
  - `prior_labels` and `labels_after` round-trip
  - each of the three validators rejects its case, and a dry-run row with an
    action and `prior_labels` is accepted
- `test_decision.py`, `effective_change`:
  - an add already present → dropped
  - a remove of `INBOX` on an archived message → dropped (M5's case)
  - both sides effective
  - names outside the decision (`STARRED`, a reader label) do not affect it
  - the existing import test still passes, unchanged
- `test_undo.py` (new):
  - `parse_window`:
    - naive time refused
    - `since >= until` refused
    - future `until` refused
    - over `MAX_WINDOW` refused
    - `Z` and `+01:00` accepted
    - `since` is inclusive and `until` exclusive
  - `select`:
    - dry-run, failure and empty-action rows are excluded
    - the window bounds hold
    - a row removing an `Agent/` label raises
    - a row adding `INBOX` raises
  - `plan_message`: each of M1–M5 as a pure case; already reverted; two rows
    for one message, unioned; a stray `Agent/` label
  - `orphans`: a dry-run row does not count as a row; an applied row outside
    the window does
  - `verify`: clean; an `Agent/` label left behind; a reader's `INBOX` change
    respected; a fallback when `labels_after` is None; a changed `STARRED`
    flagged
- `test_write_guard.py`: the caller set is exactly the two scripts.
- The scripts stay thin and are not unit-tested, the same call as
  `setup_labels` and `verify_labels`. The rehearsal is their test.

### Files touched

`app/logbook.py`, `app/decision.py` (`effective_change` only),
`app/undo.py` (new), `scripts/undo_run.py` (new),
`scripts/rehearse_undo.py` (new), `tests/test_logbook.py`,
`tests/test_decision.py`, `tests/test_undo.py` (new),
`tests/test_write_guard.py`. `DESIGN.md` →
Dry-run mode: the undo paragraphs and the `after:` epoch argument are
replaced by A3's log-driven design, and the file-tree comment is updated (A3
said this lands with step 6). No new dependencies: the code uses `pydantic`,
already present for `logbook`, and the standard library.

### The manual procedure (the human)

A. Land the code; tests pass.

B. Turn conversation view off. Send yourself six emails with the subjects
   `[personai rehearsal] M1` to `M6`, one each, so they land in six
   threads. **Archive M5.**

C. `uv run python scripts/rehearse_undo.py`. Read the plan: it should match
   the "Effective action" column above. Then run it again with `--apply`.
   Note the window it prints.

D. In Gmail: star M1. On M2, remove `Agent/Needs Review` and add
   `Agent/To Action`. Add `Agent/Processed` to M6.

E. `undo_run.py` plan-only, with the printed window and the rehearsal
   `--log`. Compare against the first expectations table and the orphan
   set. **If anything differs, stop.** Nothing has been undone yet.

F. The same, with `--apply --verify`. Compare against the second
   expectations list.

G. Run F again. Expected: all five already reverted, and zero modify calls.

H. Remove `Agent/Processed` from M6. Run E again. Expected: zero orphans,
   exit 0.

I. Delete the six rehearsal emails if you want to. Turn conversation view
   back on if you want it.

### Recorded when done

**The rehearsal passed, 2026-11-03.** All three pass criteria held, and
every expectation fixed above matched first time. These were the first
writes to real messages in the project.

- **The first run of C refused, as designed.** M5 had not been archived
  (procedure B was missed). The start-state check stopped it before any
  write, and the human archived M5 and reran it.
- **C (`--apply`):** the fixture window was `2026-11-03T07:01:31Z` to
  `07:01:35Z`, 5 rows written to
  `data/rehearsal/classifications.jsonl`.
  - Every effective change matched the fixture table.
  - M5's line read `remove -`: its decision said remove INBOX, and the
    effective change recorded nothing, which is the A3 case.
  - `labels_after` as reported by Gmail:

    | Msg | `labels_after` |
    |---|---|
    | M1 | Processed, Receipts, `IMPORTANT`, `SENT`, `UNREAD` (no INBOX) |
    | M2 | INBOX, Needs Review, Processed, `IMPORTANT`, `SENT`, `UNREAD` |
    | M3 | INBOX, Error, `IMPORTANT`, `SENT`, `UNREAD` |
    | M4 | INBOX, Processed, To Action, `IMPORTANT`, `SENT` |
    | M5 | Processed, Receipts, `IMPORTANT`, `SENT`, `UNREAD` (no INBOX) |

- **D, by hand:** M1 starred; M2 re-filed from Needs Review to To Action;
  `Agent/Processed` put on M6.
- **E (plan only):** matched the first expectations table exactly.
  - M2's To Action was reported as stray. Its Needs Review was already gone,
    so it was not listed, which shows the plan works against the current
    labels.
  - M5 had no add-back.
  - One orphan, M6 (`1a1a030c836f74fd`). Exit 1.
- **F (`--apply --verify`):** `5 changed, 0 already reverted, 0 held,
  0 failed`; `5 ok, 0 mismatched`; exit 1 for the orphan. Report:
  `data/undo/20261103T070734-af5a8b.jsonl`.
  - The human confirmed in Gmail: M1–M4 in the inbox with M1 still starred,
    M5 still archived, and M6 still carrying `Agent/Processed`.
- **G (F again):** all five `already reverted`, `0 changed`, `5 ok`, exit 1
  for the orphan. Pass criterion 2.
- **H:** after M6's label was removed by hand: `Orphans: none.`, all five
  already reverted, `5 ok`, `Clean.`, exit 0. Pass criterion 3.
  - *Run with `--apply --verify`* rather than the plan-only run the
    procedure gave. That is a superset: it made zero calls, and it verified
    as well.

**Not predicted:**
- **Starring adds two system labels,** `STARRED` and `YELLOW_STAR`. Both
  survived undo untouched, which tests "an inverse, not an overwrite"
  against a change of more than one label.
- **Mail sent to self carries `SENT` and `IMPORTANT`** alongside `INBOX`.
  Neither interacts with anything the agent owns.

**`labels_after` against a re-fetch** was not compared directly. D changed
M1, M2 and M6 before the next fetch, and F's re-fetch came after undo. The
evidence is indirect but consistent. M3 and M4 were untouched by D, and
E's plan for them (remove exactly what `labels_after` showed the agent
added) agrees. Every verify, which uses `labels_after` for its INBOX rule,
passed. Step 7's live run is where to compare them directly.

**What this unlocks:** step 7. `app/agent.py` may now join
`MODIFY_LABELS_CALLERS`. That change, and the change to
`test_no_app_module_writes_until_the_rehearsal_passes`, belong in step 7's
own diff.

### Decisions taken (step 6 review, 2026-11-03)

All agreed as recommended. Q4 and Q7 changed during review; see their rows.

| # | Decision | Choice | Why, and what was rejected |
|---|---|---|---|
| 1 | How the rehearsal applies labels | **By script** (`rehearse_undo.py`, through `modify_labels`), not by hand. Amends §6 | Hand-applying means the fixture is told the prior state, so `prior_labels` is typed, not fetched, and the write half goes unrehearsed until a live run. Cost: a second `modify_labels` caller before the pass, which step 5 anticipated; the subject tag bounds it to throwaway mail |
| 2 | Where rehearsal rows go | **A separate log**, `data/rehearsal/classifications.jsonl`, via `--log` (default `LOG_PATH`) | The rows carry synthetic distributions and must never reach a metric. *Rejected:* `RunKind "rehearsal"` in the real log, which leaves fake rows there permanently |
| 3 | `labels_after` | **Added now.** Not in §6 | Makes "ignoring the reader's changes" computable for `INBOX`, and it is lossy: absent from every earlier row. *Rejected:* re-deriving it at undo time, which cannot tell the agent's change from the reader's |
| 4 | What undo plans against | **The current labels**, fetched per message | Honest reporting, not safety: the mailbox ends up the same either way, but undo says what actually happened, and a rerun makes zero calls. Free, because `stray` and `verify` fetch anyway. *Rejected:* blind replay; also, skipping rows from earlier `data/undo/` reports. *Changed at review:* the draft overstated this as a safety property; the table in Part 2 is the corrected argument |
| 5 | Several rows for one message | **Earliest `prior_labels` against now for `Agent/` labels; "did any row archive it" for `INBOX`**; a message is **held** if a row re-added a label already in the earliest `prior_labels`. All after `select` refuses any row that is not agent-shaped | Agent-shaped rows make repeats add nothing. *Changed during build* from a union of inverses: the same result, plus one correction, and plainer (see Part 2). The hold keeps a second record checking `prior_labels`. *Rejected:* refusing every multi-row message. Also rejected: restoring `INBOX` to its earliest state, which overrides the reader. Phase 4's reconciliation breaks the shape; `select` makes that loud |
| 6 | Maximum window | **`MAX_WINDOW = 24h`**, a module constant Phase 4 revisits, like `LIVE_LIMIT_CAP` | A required window stops an accidental unscoped undo, but not `--since 2020-01-01`. *Rejected:* a confirmation prompt; plan-by-default already does that job |
| 7 | Where the effective change lives | **`decision.effective_change`, names only, returning tuples** | `logbook` would never call it; deciding what actually changes is the last step of deciding. Names keep ids out of `decision`, and tuples keep its import test unchanged. *Changed at review* from `logbook.effective_action`. *Rejected:* `logbook`; `app/undo.py` (the agent importing from undo reads backwards); a new one-function module |
| 8 | The fixture | **Six messages**: §6's mix, plus M5 archived beforehand, M6 a planted orphan, and reader changes on M1 and M2 | Each tests a claim §6's five would not exercise. M5 is A3's motivating case; M6 is the only way the sweep is seen to fail |
| 9 | `rehearse_undo.py` after the pass | **Kept**, still on `MODIFY_LABELS_CALLERS` | The subject-tag gate confines it to throwaway mail, and it keeps the rehearsal repeatable if undo changes in Phase 4 |
| 10 | Validators on `Record` | **All three**: action requires `prior_labels`; error requires an empty action; `labels_after` requires `dry_run=False` | No rows exist, so this is free now and a migration later. *Rejected:* checking them only in the agent, where the fixture could skip them |
| 11 | Single-message undo (`--message ID`) | **Not added** | A single misfiled message is fixed by hand in Gmail, which is a correction (Phase 4), not an undo. Undo reverses runs, and nothing in Phase 3 needs it narrower |

---

## Step 7 implementation

**Status: built 2026-11-04 (589 tests pass); procedure B passed 2026-11-04.** Code and tests only.
Nothing calls `classify_and_label` until step 8's `run_pass.py`, so this
step makes no Gmail call and touches no message. Where this differs from
§7, this section is newer, and each difference is marked. All ten design
questions were agreed as recommended; see [the decisions
table](#decisions-taken-step-7-review-2026-11-04) at the end of this
section.

### What reading the code turned up

- **Gmail errors arrive as `HttpError`, and `agent.py` may not import it.**
  `_execute` re-raises `googleapiclient`'s `HttpError` unchanged, and guard
  rule 1 keeps `googleapiclient` out of every module but `gmail_client`.
  `undo_run.py` gets round this with `except Exception` after re-raising
  auth errors. That is fine in a script that only reports. In the agent it
  would turn a bug in our own code, such as a `Record` validator's
  `ValueError` or a `KeyError` in the id map, into a failure row. The run
  would then carry on and write the same failure for every message (Q2).
- **Transport errors are not `HttpError`.** A socket timeout, a DNS failure
  or a reset connection comes from the HTTP layer underneath, and
  `_execute` does not retry these at all.
- **`classifier.DEFAULT_TIMEOUT` is 600 s.** With 5 tries, a hung Ollama
  could hold one message for 50 minutes (Q5).
- **§7 step 6 says "because the call is atomic nothing was applied".** That
  is not quite right. Atomic means all or nothing, not nothing. A modify
  that times out or returns a 5xx *after* Gmail committed it has applied
  everything, and we cannot tell which happened (Q6).
- **`Source` is required, but a fetch failure happens before routing,** so
  none of its values is true for that row (Q3).
- **`classify_and_label` takes any message id.** The queue query excludes
  `STOP_LABELS`, but the function has no guard of its own. On a message
  already carrying `Processed` it would add a second category label. On an
  `Agent/Error` message it would put `Processed` beside `Error`, which the
  three-states rule forbids (Q4).
- **`prefilter_domains.json` is `[]`.** The allowlist branch cannot fire on
  real mail yet; only tests reach it. That is fine, but it means the large
  dry run will report zero prefilter rows, and this is why.
- **§7's parameter `label_ids` has the same name as `gmail_client.label_ids`,**
  the function that produces it. Undo calls the same map `ids_by_name` (Q1).
- **`prompt_id` is not in `Config`.** The shipped prompt is
  `classifier.DEFAULT_PROMPT_ID` (`v9b-bookings`). The agent passes that
  constant and logs it.
- **The eval already scored the reply rule first and the allowlist second**
  (`evalscore.py`, commented "the live order"). §7's order is the one the
  dev and holdout figures measured.

### `app/agent.py`

```python
# Q7: 4 retries after the first attempt, 5 tries in total (plan review).
OLLAMA_RETRY_DELAYS: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0)
# Q5: replaces classifier.DEFAULT_TIMEOUT for live classification.
CLASSIFY_TIMEOUT: float = 120.0

def classify_and_label(
    svc,
    message_id: str,
    *,
    cfg: Config,
    ids_by_name: Mapping[str, str],        # Q1; was `label_ids`
    allowlist: Collection[str],
    run_id: str,
    run_kind: RunKind,
    dry_run: bool,
) -> logbook.Record | None:                # None: Q4
```

**Order, per message.** These are §7's steps, with changes marked.

0. *New (Q1).* `ids_by_name` must cover `AGENT_LABELS | {INBOX}`. If it
   does not, raise `ValueError` before any call. That would be a caller
   bug. `run_pass` builds the map with `label_ids()`, which already fails
   closed; this check makes the function safe on its own.
1. `fetch` (format=full, as in eval). Keep `label_ids` as `prior_labels`.
   A `GmailApiError` becomes a failure row with `prior_labels=None` and
   `source=None` (Q3). Errors that affect the whole mailbox halt (Q2).
2. *New (Q4).* If the message already carries any `STOP_LABELS` id, return
   `None`: no row and no write.
3. **Reply rule:** `prefilter.is_reply(subject)` → `decide_reply_hit()`,
   `source="reply"`.
4. **Allowlist:** `prefilter.matches(sender, allowlist)` →
   `decide_prefilter_hit()`, `source="prefilter"`.
5. **Model:** `_classify(message, cfg)`, with retries (Q7), then `decide(...)`
   with `cfg`'s two thresholds, `source="model"`. A `ClassifierError` that
   outlasts the retries becomes `decide_failure()` with `error` set.
6. **Effective change:** turn `prior_labels` into names through the
   inverted map (ids not in it are dropped), then
   `decision.effective_change`, then `logbook.Action`.
7. If `not dry_run` and the action is non-empty: **one** `modify_labels`
   call, with ids from `ids_by_name`. Its return becomes `labels_after`. A
   `GmailApiError` becomes a failure row (Q6).
   - After step 2, every successful decision adds `Processed` or `Error`,
     and neither can already be present. So on success the action is
     always non-empty. The condition is still kept: on a failure row the
     action is empty, and `modify_labels` refuses an empty change anyway.
8. `logbook.append`, **after** the modify. Return the row.

`dry_run` is still a parameter, as §7 says, so `--live` stays the only way
to write. A dry run does everything except step 7's call, including the
effective change against the real prior labels. So the large dry run shows
exactly what a live run would have done.

**Retries (Q7).** `_classify` calls `classifier.classify(...)` with
`model=cfg.ollama_model`, `body_chars=cfg.body_chars`,
`prompt_id=DEFAULT_PROMPT_ID` and `timeout=CLASSIFY_TIMEOUT`. On
`OllamaError` it sleeps for the next delay and tries again. After the
fourth retry fails, the last error is used. `NoCategoryLetter` is never
retried. Sleeping goes through `time.sleep`, looked up on the module, so
tests patch it and record the delays instead of waiting.

**What halts the run (Q2).** These propagate and no row is written:
`NotAuthenticated`, `RefreshError`, the new `gmail_client.Forbidden`,
and anything that is not a `GmailApiError` or a `ClassifierError`: bugs,
and transport errors. Only `GmailApiError` (fetch or modify) and
`ClassifierError` (after retries) become failure rows.

**What a row carries** (Q10):

| Field | Set when |
|---|---|
| `message_id`, `run_id`, `run_kind`, `dry_run` | always |
| `sender`, `subject`, `prior_labels` | the fetch succeeded |
| `source` | routing was reached (Q3) |
| `model`, `prompt_id`, `body_chars` | the model was called, whether it succeeded or failed |
| `distribution`, `retained_mass` | the model succeeded |
| `category`, `confidence` | from the `Decision`: `confidence` is `None` on rule rows, as `decision.py` intends |
| `confidence_threshold`, `to_action_floor` | `decide()` ran |
| `action`, `labels_after` | as the validators require |
| `error` | `"<stage>: <ExceptionType>: <message>"`, where stage is `fetch`, `classify` or `modify` |
| `note` | missing letters (`"missing letters: C, F"`), Ollama retries if any (`"ollama retries: 2"`), and on a modify failure the intended change (Q6), joined with `"; "` |

The stage in `error` lets Phase 4 decide which failures count toward
dead-lettering. A deleted message's 404 probably should not count the same
as a prompt the model always fails on. That decision belongs to Phase 4;
the string only makes it possible.

### `app/gmail_client.py` (Q2)

```python
class GmailApiError(GmailError):
    """An API call that failed after _execute's backoff. `.status` is the
    HTTP status, or None if the response carried none."""

class Forbidden(GmailApiError):
    """Any 403 that outlasted the backoff: a scope problem, or a quota that
    stayed exhausted. Both are about the account, not one message."""
```

`_execute` raises one of these, `from` the original `HttpError`, where it
raises the bare `HttpError` today. Retry behaviour does not change.

- **Why every 403 is `Forbidden`, not only the permission one:** a 403
  from Gmail is always about the account. That covers scope, a daily
  limit, or a rate limit still in force after 7 attempts. Every later
  message would fail the same way, so it halts, like an auth failure.
- **Transport errors are not wrapped.** They propagate and halt the run.
  On a manual run of at most 25 that costs one restart. If one cuts off a
  modify that Gmail had already committed, the message is an orphan, and
  the sweep reports it. The poller (Phase 4) will need its own policy for
  these; that goes in `docs/BACKLOG.md`, not here.
- **Existing callers are unaffected.** `undo_run.py` catches `Exception`,
  and the other scripts catch nothing. Six tests in `test_gmail_client.py`
  assert `HttpError` and are amended to the new types, with the cause
  checked.

### `app/logbook.py`

- `Source = Literal["model", "prefilter", "reply", "correction"]`.
  `"reply"` was deferred to this step by §6.
- *Q3:* `source: Source | None`, and a fourth validator: `source is None`
  requires `error`. A row can lack a source only if it failed before
  routing.

### The guard — `tests/test_write_guard.py`

- `MODIFY_LABELS_CALLERS` gains `"app/agent.py"`. Its comment changes from
  "joins only at step 7" to "joined at step 7, after the rehearsal passed
  on 2026-11-03".
- `test_no_app_module_writes_until_the_rehearsal_passes` is **changed
  deliberately**, as step 6 said it would be (Q8). It becomes
  `test_agent_is_the_only_app_module_that_writes`:
  `{p for p in MODIFY_LABELS_CALLERS if p.startswith("app/")} == {"app/agent.py"}`.
- Two docstrings that say "until the rehearsal has passed" are updated:
  rule 4 in the module docstring, and `test_only_allowed_callers_refer_to_modify_labels`.

### Tests

`tests/test_agent.py` (new). Gmail is faked at the service level (Q9), so
the real `fetch` parsing and the real `modify_labels` checks run.
`classifier.classify` is monkeypatched, and `time.sleep` is patched to
record delays.

- **One test per row of the decision table**, run live and in a dry run
  (parametrised):
  - confident Receipts (0.95): one modify adding Receipts and Processed and
    removing `INBOX`, all as ids
  - confident To Action, and confident Bookings: no `INBOX` remove
  - Needs Review (0.55): Needs Review and Processed added, `INBOX` kept
  - the To Action override (Receipts 0.80, To Action 0.15):
    `to_action_override` is true and `INBOX` is kept
  - prefilter hit; reply hit
  - failure: no modify call

  The dry run makes zero modify calls, and its row equals the live row
  except for `dry_run` and `labels_after`.
- **Reply beats the allowlist:** a `Re:` subject from an allowlisted domain
  routes as `reply`, and `classify` is never called.
- **Rule rows** carry no model fields and no thresholds.
- **Already archived:** a confident Receipts on a message without `INBOX`
  sends no `INBOX` in `removeLabelIds`, and the row's action has no remove.
- **Terminal state (Q4):** `Processed` present returns `None`, with no
  modify and no row. The same for `Error`.
- **Failures:**
  - `NoCategoryLetter`: one call, no sleep, a failure row
  - `OllamaError` five times: five calls, sleeps of 2, 4, 8 and 16 s, a
    failure row
  - `OllamaError` twice then success: three calls, the message labelled,
    `note` says `ollama retries: 2`
  - fetch 404: a failure row with `prior_labels=None` and `source=None`
  - modify 500 (after backoff): a failure row with stage `modify`, an empty
    action, and the intended change in `note`
- **Halts:** `NotAuthenticated` from fetch, `RefreshError` from modify and
  `Forbidden` from fetch each propagate, and no row is written.
- **A bug is not a failure:** `classify` raising `KeyError` propagates, and
  no row is written. This is the test that `except Exception` would fail.
- **The row is written after the modify:** the fake's `modify` asserts the
  log file is still empty when it is called.
- **A map missing a label** raises `ValueError` before any fetch.
- **Logged parameters:** `prompt_id == DEFAULT_PROMPT_ID`, and `model`,
  `body_chars` and both thresholds come from `cfg`.

Elsewhere:

- `test_logbook.py`: `"reply"` is accepted; `source=None` without `error`
  is rejected; with `error` it is accepted.
- `test_gmail_client.py`: the six `HttpError` assertions are amended. New
  tests: a 404 raises `GmailApiError` with `.status == 404` and the
  `HttpError` as its cause; a permission 403 raises `Forbidden` after one
  attempt; a rate-limit 403 that outlasts the backoff raises `Forbidden`.
- `test_write_guard.py`: as above.

### Not in this step

- No entry point and no live call. Step 8's `run_pass.py` is the first
  caller.
- No dead-lettering and no failure counting. Both are Phase 4.
  `decide_dead_letter()` stays unreachable from the agent.

### Files touched

`app/agent.py` (new), `app/logbook.py`, `app/gmail_client.py`,
`tests/test_agent.py` (new), `tests/fakes.py` (new, Q9),
`tests/test_gmail_client.py`, `tests/test_logbook.py`,
`tests/test_write_guard.py`. One line in `docs/BACKLOG.md`: transport
errors in the poller. No new dependencies: `RefreshError` comes from
`google-auth`, already present and already imported by `auth.py` and
`undo_run.py`.

### Changed during build, 2026-11-04

Where building settled a detail this section left open:

- **The modify-failure row.** The table above says `distribution` is set
  when "the model succeeded", which a modify failure satisfies. Built to the
  older rule in `logbook` instead: "a row with an error is never also a
  classification". So a modify-failure row drops `category`, `confidence`,
  `distribution`, `retained_mass` and the thresholds, and keeps `model`,
  `prompt_id` and `body_chars`, since the model was called. What was lost
  goes in `note`, e.g. `intended: Receipts; +Agent/Processed +Agent/Receipts
  -INBOX`, which is what an orphan investigation needs. The message retries
  next run and is classified again anyway.
- **`CLASSIFY_TIMEOUT = 120`**, an int, because `classifier.classify`'s
  `timeout` is typed `int`.
- **Fakes.** `FakeMessages` gained `get_request` (alongside
  `modify_request`) so a fetch can fail, and `FlakyRequest` accepts any
  exception, so the halt tests can raise `RefreshError` and `TimeoutError`
  from inside a call.
- **One test beyond the list:** a `TimeoutError` from fetch halts the run,
  which is the transport-error half of Q2.
- **The tests were checked by breaking the code.** Three changes were made to
  `agent.py` and then reverted, and each was caught: `except Exception` on
  the classify call (1 test failed), the allowlist moved before the reply
  rule (4 failed), and the terminal-state check removed (2 failed).
- **`docs/BACKLOG.md`** gained "Transport errors — no policy for the poller
  yet", and a paragraph in the quota section: a rate-limit 403 that outlasts
  the backoff now halts a run instead of failing each message.

### The manual procedure (the human)

A. Land the code; tests pass. **Done 2026-11-04:** 589 pass (545 before
   this step), ruff clean.

B. **Check `CLASSIFY_TIMEOUT` against a cold start.** Run
   `ollama stop llama3.1:8b`, then `uv run python scripts/smoke_classify.py`,
   and note the first email's wall time. That is model load plus one
   classification. This reads no mail and writes nothing.
   *Fixed here before running:* if the cold start is ≤ 40 s, the timeout
   stays at 120 s, a 3× margin. Otherwise it becomes 3× the measured value,
   rounded up to the next 30 s, and this section is amended.

### Recorded when done

- The cold-start time from B, 2026-11-04: **22.2 s** (model load plus one
  classification); warm calls 1.0-2.5 s. That is under 40 s, so
  `CLASSIFY_TIMEOUT` stays at 120 s.
- The test count: 589.

### Design questions (as reviewed)

**Q1 — The label map's name and coverage.** *Recommend:* rename §7's
`label_ids` to `ids_by_name`, the name undo already uses, and check
coverage of `AGENT_LABELS | {INBOX}` on entry. A parameter with the same
name as the function that builds it shadows that function inside the
module that needs it. *Rejected:* trusting the caller alone. The check
costs one set comparison per message.

**Q2 — Which errors halt, and how the agent tells them apart.**
*Recommend:* `_execute` raises `GmailApiError` (with `Forbidden` for any
403) in place of `HttpError`. The agent turns `GmailApiError` and
`ClassifierError` into failure rows, and lets everything else propagate:
auth, `Forbidden`, transport errors, bugs. *Rejected:*
- `except Exception`, as in `undo_run.py`. A bug in our code would become
  a stream of quiet failure rows. Phase 4's poller would then retry them
  forever.
- Importing `HttpError` into the agent. That breaks guard rule 1.
- Wrapping transport errors as well. Halting costs one restart on a run of
  at most 25, and catching them needs an import of the HTTP library
  underneath (`httplib2`), a dependency we use without naming.

**Q3 — The source of a row that failed before routing.** *Recommend:*
`source: Source | None`, with `None` allowed only when `error` is set.
*Rejected:*
- `"model"`. It claims a route that was never taken.
- A new `"none"` value. `None` already means "absent", and a sentinel
  string would have to be filtered out of every count by source.

Free now, because the real log has no rows.

**Q4 — A message already in a terminal state.** *Recommend:* check after
the fetch, and return `None` with no write and no row. `run_pass` reports
it as skipped. This makes the three-states rule a property of the writer
rather than of the query, which a race between two runs or a bad
`--query` could get round. *Rejected:*
- A failure row. Phase 4's dead-letter counting would count it, and could
  put `Error` beside `Processed`.
- A "skipped" row. The log records classifications, and this is not one.

**Q5 — The classify timeout.** *Recommend:* `CLASSIFY_TIMEOUT = 120.0`,
checked against a cold start by procedure B. A warm classification takes
about 2.6 s. *Rejected:*
- Keeping 600 s, which makes the worst case for one message 50 minutes.
- Something like 30 s, which a cold model load on this CPU might exceed.
  That would turn every first message of a run into a retry.

**Q6 — A modify that fails ambiguously.** *Recommend:* write a failure
row (empty action, as the validator requires) with the intended change in
`note`, and do not re-fetch. If the call had in fact landed, the message
carries `Processed` with no applied row. The sweep reports that as an
orphan, and the note says what it probably received. §7's "nothing was
applied" becomes "probably nothing; if something was, the sweep reports
it". *Rejected:* re-fetching to find out. That puts a second call in the
failure path, which can itself fail, and the sweep already covers the case.

**Q7 — The `OllamaError` retry delay**, which §7 left open. *Recommend:*
2, 4, 8 and 16 s, so 30 s across one message's five tries, retrying only
`OllamaError`. The transient case this is for is Ollama restarting or
dropping a connection; a restart takes seconds, and 30 s covers it with
margin. *Rejected:*
- A fixed 1 s. All five tries would be over in 4 s, sooner than a
  restart.
- Jitter. There is one client, so there is nothing to spread out.
- Retrying `NoCategoryLetter`. For the same input the distribution is
  reproducible (Phase 0 measured it identical across temperatures), so a
  retry asks the same question and gets the same answer.

Cost, accepted: with Ollama fully down, each message spends about 30 s
before its failure row. On a run of 25 that is about 12 minutes, which the
per-line output makes visible, and the human can stop it.

**Q8 — The rehearsal test.** *Recommend:* replace its assertion ("no
`app/` module writes") with "exactly `app/agent.py` writes from `app/`",
and rename it. A second `app/` module that starts writing still fails a
test, which is what the original was protecting. *Rejected:* deleting it,
which loses that protection.

**Q9 — How Gmail is faked.** *Recommend:* move `FakeService` and its
parts from `test_gmail_client.py` into `tests/fakes.py`. The agent tests
then run the real `fetch` and `modify_labels` against them. A test where
the agent passes names instead of ids then fails on `modify_labels`' own
check. *Rejected:*
- Monkeypatching `fetch` and `modify_labels` with recorders. A recorder
  accepts whatever it is given.
- Importing the fakes from `test_gmail_client.py`. One test module
  depending on another breaks when that module is reorganised.

**Q10 — What a rule row records.** *Recommend:* only what was used. Rule
rows carry no `model`, `prompt_id`, `body_chars` or thresholds, because
none of them decided the row (see the table above). *Rejected:* recording
the config on every row. A reply row carrying `confidence_threshold=0.8`
would read as if a threshold had been applied. `run_id` already ties
every row to the run's config.

### Decisions taken (step 7 review, 2026-11-04)

All agreed as recommended.

| # | Decision | Choice |
|---|---|---|
| 1 | The label map | Renamed `ids_by_name`; coverage of `AGENT_LABELS \| {INBOX}` checked on entry |
| 2 | Errors | `_execute` raises `GmailApiError`, or `Forbidden` for any 403, in place of `HttpError`. `GmailApiError` and `ClassifierError` become failure rows; auth, `Forbidden`, transport errors and bugs halt the run |
| 3 | Source before routing | `source: Source \| None`; `None` only with `error` set (a fourth validator) |
| 4 | Terminal-state message | Return `None`: no write, no row |
| 5 | Classify timeout | `CLASSIFY_TIMEOUT = 120.0`, checked by procedure B |
| 6 | Ambiguous modify failure | A failure row with the intended change in `note`, and no re-fetch. If the call landed, the message carries `Processed` with no applied row, and the sweep reports it as an orphan. Amends §7's "nothing was applied" |
| 7 | `OllamaError` retry delay | 2, 4, 8, 16 s; `OllamaError` only, never `NoCategoryLetter` |
| 8 | The rehearsal test | Replaced by `test_agent_is_the_only_app_module_that_writes` |
| 9 | Gmail fakes | Moved to `tests/fakes.py`; agent tests run the real `fetch` and `modify_labels` |
| 10 | Rule rows | Record only what was used: no model fields, no thresholds |
