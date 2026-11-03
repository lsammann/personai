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

**Current step: 3** (not started).

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

**Status: proposed, 2026-11-03. Not yet agreed.** Still no message writes by
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

- Both runs' outputs.
- Whether `label:"Agent"` matches child-labelled messages.
- That the hand-applied labels were removed, which is shown by the `clean`
  run.
