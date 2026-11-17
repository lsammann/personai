#!/usr/bin/env python3
"""Write the undo rehearsal's fixture: real label changes on throwaway mail.

    rehearse_undo.py            print what it would do; change nothing
    rehearse_undo.py --apply    label M1-M5 and write their log rows

Phase 3 step 6, Part 3. It changes labels through `modify_labels` the way
step 7's agent will, so the rehearsal tests the write half as well as undo:
for each message, fetch (keep `prior_labels`), take a fixed decision from
the real `decide_*` functions, `decision.effective_change`, ONE
`modify_labels` call, then append the log row - after the modify.

**It cannot touch real mail.** It acts only on messages whose subject is
exactly `[personai rehearsal] M<n>`, found by search, and refuses to run
unless M1-M6 each appear exactly once and nothing untagged matches. There is
no `--ids` or `--query`: the tag is the only way in. It also refuses unless
each message is in the state the rehearsal starts from (see `check_start`).

Rows go to `data/rehearsal/classifications.jsonl`, never to the real
classification log: their distributions are synthetic and must never reach
a metric. M6 gets nothing - it is the planted orphan, labelled by hand.

When done, prints the exact `undo_run.py` command for the window it wrote.
Kept after the rehearsal passes so it can be rerun if undo changes (step 6
decision Q9). Fixture and expectations: docs/PHASE3_PLAN.md, Step 6, Part 3.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from dataclasses import dataclass

from app import auth, config, decision, gmail_client, logbook
from app.categories import (
    AGENT_LABELS,
    ERROR,
    INBOX,
    NEEDS_REVIEW,
    PROCESSED,
    REQUIRED_LABELS,
    Category,
    label_for,
)
from app.decision import Decision

REHEARSAL_LOG = config.DATA_DIR / "rehearsal" / "classifications.jsonl"
TAG = "[personai rehearsal]"
SEARCH = f'subject:"{TAG}" -in:drafts'
ALL_TAGS = tuple(f"{TAG} M{n}" for n in range(1, 7))
ORPHAN_TAG = f"{TAG} M6"


def _distribution(winner: Category, p: float) -> dict[Category, float]:
    """`winner` at p, the rest spread evenly. Synthetic: it exercises the
    decision table, not the model."""
    rest = (1.0 - p) / (len(Category) - 1)
    return {category: (p if category is winner else rest) for category in Category}


@dataclass(frozen=True)
class Fixture:
    tag: str
    in_inbox: bool  # the state it must start in
    distribution: dict[Category, float] | None  # None: dead letter
    # What the plan's table says the decision is, checked before any write.
    expect_add: frozenset[str]
    expect_remove: frozenset[str]


def _fixtures() -> list[Fixture]:
    """M1-M5, as in the plan's fixture table."""
    receipts = label_for(Category.RECEIPTS)
    to_action = label_for(Category.TO_ACTION)
    return [
        Fixture(f"{TAG} M1", True, _distribution(Category.RECEIPTS, 0.95),
                frozenset({receipts, PROCESSED}), frozenset({INBOX})),
        Fixture(f"{TAG} M2", True, _distribution(Category.UPDATES, 0.55),
                frozenset({NEEDS_REVIEW, PROCESSED}), frozenset()),
        Fixture(f"{TAG} M3", True, None, frozenset({ERROR}), frozenset()),
        Fixture(f"{TAG} M4", True, _distribution(Category.TO_ACTION, 0.95),
                frozenset({to_action, PROCESSED}), frozenset()),
        # Archived by hand first: the case A3 exists for. Its decision says
        # remove INBOX; its effective change must not.
        Fixture(f"{TAG} M5", False, _distribution(Category.RECEIPTS, 0.95),
                frozenset({receipts, PROCESSED}), frozenset({INBOX})),
    ]


def _decide(fixture: Fixture, cfg: config.Config) -> Decision:
    if fixture.distribution is None:
        return decision.decide_dead_letter()
    return decision.decide(
        fixture.distribution,
        confidence_threshold=cfg.confidence_threshold,
        to_action_floor=cfg.to_action_floor,
    )


def find_messages(svc) -> dict[str, gmail_client.Message]:
    """tag -> message, or SystemExit saying what is wrong."""
    found: dict[str, list[gmail_client.Message]] = {}
    for message_id in gmail_client.search_ids(svc, SEARCH):
        message = gmail_client.fetch(svc, message_id)
        if message.subject not in ALL_TAGS:
            raise SystemExit(
                f"Refusing: {message_id} matches the search but its subject is "
                f"{message.subject!r}, not one of the six tags."
            )
        found.setdefault(message.subject, []).append(message)
    problems = [
        f"{tag}: found {len(found.get(tag, []))}, need exactly 1"
        for tag in ALL_TAGS if len(found.get(tag, [])) != 1
    ]
    if problems:
        raise SystemExit("Refusing:\n  " + "\n  ".join(problems))
    return {tag: messages[0] for tag, messages in found.items()}


def check_start(fixture: Fixture, message: gmail_client.Message,
                agent_ids: set[str]) -> str | None:
    """Why this message is not in the state the rehearsal starts from."""
    if agent_ids & set(message.label_ids):
        return "already carries an Agent/ label - not a fresh fixture"
    if (INBOX in message.label_ids) != fixture.in_inbox:
        return "should be archived (procedure B)" if not fixture.in_inbox else "should be in the inbox"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="label M1-M5 and log them")
    args = parser.parse_args()

    auth.require_scopes(auth.MODIFY if args.apply else auth.READONLY)
    svc = gmail_client.service()
    ids_by_name = gmail_client.label_ids(svc, sorted(REQUIRED_LABELS))
    names_by_id = {label_id: name for name, label_id in ids_by_name.items()}
    agent_ids = {ids_by_name[name] for name in AGENT_LABELS}
    cfg = config.get()

    messages = find_messages(svc)
    if agent_ids & set(messages[ORPHAN_TAG].label_ids):
        print("Refusing: M6 already carries an Agent/ label. It is labelled by "
              "hand AFTER this script runs (procedure D).")
        return 1

    # Everything is checked before the first write, so a refusal changes nothing.
    planned = []
    for fixture in _fixtures():
        message = messages[fixture.tag]
        problem = check_start(fixture, message, agent_ids)
        if problem:
            print(f"Refusing: {fixture.tag} {problem}.")
            return 1
        chosen = _decide(fixture, cfg)
        if (set(chosen.labels_add), set(chosen.labels_remove)) != (fixture.expect_add, fixture.expect_remove):
            print(f"Refusing: {fixture.tag} decided {chosen.labels_add} / -{chosen.labels_remove}, "
                  "not what the plan's fixture table says. Has the config changed?")
            return 1
        prior_names = [names_by_id[i] for i in message.label_ids if i in names_by_id]
        add, remove = decision.effective_change(chosen, prior_names)
        planned.append((fixture, message, chosen, add, remove))
        print(f"  {fixture.tag}  {message.id}\n      add {', '.join(add) or '-'}"
              f"   remove {', '.join(remove) or '-'}")
    print(f"  {ORPHAN_TAG}  {messages[ORPHAN_TAG].id}\n      nothing (the planted orphan)")

    if not args.apply:
        print("\nPlan only. Rerun with --apply to label M1-M5.")
        return 0

    run_id = logbook.new_run_id()
    started = dt.datetime.now(dt.UTC)
    print(f"\nApplying. Log: {REHEARSAL_LOG}")
    for fixture, message, chosen, add, remove in planned:
        # Stop at the first failure: modify is atomic, so the failed message
        # is untouched, and undo can revert every row already written.
        after = gmail_client.modify_labels(
            svc, message.id,
            add_ids=[ids_by_name[name] for name in add],
            remove_ids=[ids_by_name[name] for name in remove],
        )
        logbook.append(
            logbook.Record(
                run_id=run_id, run_kind="manual", message_id=message.id,
                sender=message.sender, subject=message.subject, source="model",
                category=chosen.category, confidence=chosen.confidence,
                distribution=fixture.distribution or {},
                confidence_threshold=cfg.confidence_threshold,
                to_action_floor=cfg.to_action_floor,
                prior_labels=message.label_ids, labels_after=after,
                action=logbook.Action(add=add, remove=remove),
                dry_run=False, note="rehearsal fixture",
            ),
            REHEARSAL_LOG,
        )
        print(f"  {fixture.tag}  labelled; now {sorted(names_by_id.get(i, i) for i in after)}")

    since = started.replace(microsecond=0)
    until = dt.datetime.now(dt.UTC).replace(microsecond=0) + dt.timedelta(seconds=1)
    print(
        "\nDone. Now procedure D by hand, then:\n"
        f"  uv run python scripts/undo_run.py --since {since:%Y-%m-%dT%H:%M:%SZ} "
        f"--until {until:%Y-%m-%dT%H:%M:%SZ} --log {REHEARSAL_LOG.relative_to(config.REPO_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
