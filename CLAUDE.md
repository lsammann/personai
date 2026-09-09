# CLAUDE.md

Personal Gmail classification agent. Read `DESIGN.md` for what and why,
`docs/PLAN.md` for build order and phase gates.

**Current phase: 2 (eval harness).** This is the single source of truth for
where the project is - `docs/PLAN.md` describes the phases and records what
each one found, but never claims which is current.

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
