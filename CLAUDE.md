# CLAUDE.md

Personal Gmail classification agent. Read `DESIGN.md` for what and why,
`docs/PLAN.md` for the phases and their gates, and the current phase's own
plan - `docs/PHASE2_PLAN.md` - for how this phase is being built and where the
code has deliberately diverged from it.

**Current phase: 2 (eval harness), step 8 of 8.**

This file is the single source of truth for which **phase** is current;
`docs/PHASE2_PLAN.md` → Build order is the single source of truth for which
**step**, and records what each finished step produced. Check the build order
before planning anything - steps 1-6 are done (200 hand-labelled messages, a
working harness, and the sweeps finished), and the phase plan's §1-§6 describe
the phase as originally agreed, not as it stands. **Step 6 results** near the
end of that document holds the measured configuration and three findings that
change the design: the `to_action_floor` is inert on this model, `Bookings`
now keeps `INBOX`, and `body_chars` should be 300 rather than 1500. Step 3 also added a **labelling rules** section,
which is the decision procedure ground truth was built with and the one step
7's recheck has to reapply.

Start there rather than at the top of `DESIGN.md`: the phase plan's
**amendment log** lists every place the build knowingly departed from the
agreed design, which is the context that would otherwise be lost between
sessions.

## How we work

This project is a learning exercise as much as a tool. The point is to
understand custom AI systems well enough to defend every decision, so
throughput is not the goal and generated code I have not reasoned about is
worth nothing here.

- **Plan before code. Always.** Propose the design - module boundaries,
  function signatures, the decision logic, what gets tested - and get
  agreement before writing anything. No jumping from "shall we start?" to a
  finished file.
- **Plan before each phase**, not just each file. `docs/PLAN.md` says what a
  phase contains; the plan says how it will be built and why.
- **Strictly pair programming.** Architecture and reasoning first,
  implementation second. Explain trade-offs and name the alternatives that
  were rejected. Surface design questions rather than quietly deciding them
  while writing code.
- **Justify every third-party library.** State specifically what the standard
  library cannot do, or does badly enough to matter. "It's conventional" is
  not a reason. This applies to dependencies already present as much as to
  new ones.
- **Never run git commits.** Every commit is made by the human, after reading
  the diff. Do not stage, commit, push, or amend. Report what changed and
  leave it in the working tree.

## Hard rules

- **Scope is `gmail.readonly` until Phase 3.** Do not request or use
  `gmail.modify` before then. Phases 0–2 must be incapable of modifying the
  inbox, not merely unlikely to.
- **Never commit `data/`.** OAuth credentials, tokens, and the classification
  log live there. Check `.gitignore` before adding files.
- **No write path ships before `scripts/undo_run.py` exists and has been run
  successfully** against real messages. See Phase 3.
- **`undo_run.py` takes a required time window with no default.** An unscoped
  undo would restore thousands of correctly-archived emails to the inbox.
- **Every Gmail label change is one atomic `messages.modify` call** with
  `addLabelIds` and `removeLabelIds` together — never sequential calls.
- **Confidence comes from the renormalised first-token distribution, never
  from asking the model for a number.** Phase 0 measured self-reported
  confidence as carrying no signal on any model tested (one was inversely
  calibrated; another emitted `0.900` for every email). The classification
  call emits a single letter with `logprobs: true` and no chain-of-thought.
  See DESIGN.md → Classification logic before changing this.
- **Failure ≠ low confidence.** A failure applies *no* labels (not even
  `Agent/Processed`) so the message retries. Low confidence applies
  `Agent/Needs Review` + `Agent/Processed`. Never collapse these.
- **Three mutually exclusive mailbox states**: `Agent/Processed`,
  `Agent/Error`, or neither. A dead-lettered message gets `Agent/Error`
  *alone* — never alongside `Agent/Processed` — and keeps `INBOX`. Every
  queue query excludes both, built from `STOP_LABELS` in `app/categories.py`
  rather than retyped. `undo_run.py` must sweep both.
- **Don't build anything from Future Enhancements.** If it gets tempting,
  write it down there and move on.

## Conventions

- `uv` for dependencies (`uv add`, `uv run`), `pytest` for tests
- Label names use **spaces** — `Agent/To Action`, not `Agent/To-Action`.
  Every Gmail query is built through `categories.label_term()`, which
  quotes, so a label name never has to be formatted into a query string at
  a call site.
- `app/decision.py` stays pure — no network, no I/O. It's the most-tested
  file in the project and that only holds if it stays isolated; a test parses
  its imports and fails if anything else creeps in.
- `app/classifier.py` separates the pure part — turning a raw top-20 logprob
  list into a normalised distribution over the six categories — from the
  Ollama call, which is mocked in tests
- `app/main.py` is routes only. Logic belongs in `agent.py`.
- Gmail and Ollama are mocked in tests. Model quality is measured by the eval
  set, never asserted in unit tests — they answer different questions.

## Environment

- CPU-only inference (Ryzen 5 7540U, 6c/12t, no usable GPU). The
  classification call emits a single token, so latency is essentially
  prompt length ÷ prompt-eval rate — body truncation is the dominant
  performance lever, not a detail. Real bodies have a median of ~7,400
  chars; see `docs/BACKLOG.md`.
- Ollama runs locally on the default port.
