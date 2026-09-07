# CLAUDE.md

Personal Gmail classification agent. Read `DESIGN.md` for what and why,
`docs/PLAN.md` for build order and phase gates.

**Current phase: 0 (foundations & spikes).**

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
- **Failure ≠ low confidence.** A failure applies *no* labels (not even
  `Agent/Processed`) so the message retries. Low confidence applies
  `Agent/Needs-Review` + `Agent/Processed`. Never collapse these.
- **Don't build anything from Future Enhancements.** If it gets tempting,
  write it down there and move on.

## Conventions

- `uv` for dependencies (`uv add`, `uv run`), `pytest` for tests
- Label names use **hyphens**: `Agent/To-Action`, not `Agent/To Action`
- `app/rules.py` stays pure — no network, no I/O. It's the most-tested file
  in the project and that only holds if it stays isolated.
- `app/classifier.py` separates `parse_response()` (pure, tested) from the
  Ollama call (mocked in tests)
- `app/main.py` is routes only. Logic belongs in `agent.py`.
- Gmail and Ollama are mocked in tests. Model quality is measured by the eval
  set, never asserted in unit tests — they answer different questions.

## Environment

- CPU-only inference (Ryzen 5 7540U, 6c/12t, no usable GPU). Prompt length
  dominates latency, so body truncation and `reasoning` length are
  performance decisions as much as quality ones.
- Ollama runs locally on the default port.
