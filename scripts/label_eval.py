#!/usr/bin/env python3
"""Build and label the Phase 2 eval set.

    label_eval.py sample --seed 7      fix which 200 messages get labelled
    label_eval.py label                hand-label them, resumable
    label_eval.py verify               check the set is whole and consistent
    label_eval.py recheck              re-label a blind subset: the ceiling

Sampling and labelling are separate subcommands on purpose. The frame is then
fixed and auditable, and resuming a labelling session is `sampled - labelled`
rather than a re-derived query that might have drifted since.

**Two rules for the labeller, and they are both about what ground truth means:**

Label what the email *should have been when it arrived* - not what it is today.
A concert ticket that needed buying last March is `To Action` even though the
concert has been and gone.

The truncation marker is informational only. Ground truth is what the email
*is*, not what the model can see. If the target moved with `--body-chars`,
every truncation change would silently redefine what is being measured and
`body_chars` would stop being tunable at all. Press `m` if the visible part is
not enough to decide.

Read-only. This script lists and gets; it cannot touch a label, and the token
carries `gmail.readonly` besides.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import termios
import tty
from collections import Counter, deque
from dataclasses import replace
from pathlib import Path

from app import evallabel, evalscore, evalset, gmail_client
from app.evallabel import KEYS

CACHE_DIR = evalset.DATA_DIR / "eval_cache"

RULE = "-" * 78


# --- sample ---------------------------------------------------------------


def cmd_sample(args: argparse.Namespace) -> int:
    spec = evalset.FrameSpec.ending(args.end, args.window_years)
    print(f"window: {spec.start} to {spec.end}")
    print(f"query:  {spec.query}")

    frame = [] if args.refresh else evalset.load_frame(spec)
    if frame:
        print(f"frame:  {len(frame)} ids from cache ({evalset.FRAME_PATH})")
        svc = None
    else:
        svc = gmail_client.service()
        print("enumerating frame...")
        frame = evalset.enumerate_frame(svc, spec)
        evalset.save_frame(frame, spec)
        print(f"frame:  {len(frame)} ids -> {evalset.FRAME_PATH}")

    if not frame:
        print("empty frame - nothing to sample", file=sys.stderr)
        return 1

    # Membership needs Gmail even when the frame came from cache: the stratum
    # queries are what say which cell each id belongs to.
    svc = svc or gmail_client.service()
    print("resolving strata...")
    membership = evalset.resolve_membership(svc, frame, spec=spec)

    plan = evalset.build_plan(
        frame,
        membership,
        seed=args.seed,
        extend=evalset.make_extender(svc, spec),
        spec=spec,
    )

    evalset.save_sample(plan)
    evalset.save_strata(plan)
    report(plan)
    return 0


def report(plan: evalset.SamplePlan) -> None:
    counts = evalset.sampled_counts(plan)
    print(f"\nframe {plan.frame_size}  seed {plan.seed}  sampled {len(plan.sampled)}")
    print(f"{'stratum':<12}{'N_h':>8}{'n_h':>7}{'ext':>6}{'yrs':>5}{'w_h':>9}")
    for stratum in plan.strata:
        n_h = counts[stratum.name]["n_h"]
        big_n = plan.counts.get(stratum.name, 0)
        weight = f"{big_n / n_h:.1f}" if n_h else "-"
        print(
            f"{stratum.name:<12}{big_n:>8}{n_h:>7}"
            f"{counts[stratum.name]['n_extension']:>6}"
            f"{plan.windows.get(stratum.name, 1):>5}{weight:>9}"
        )

    draws = {}
    splits = {}
    for row in plan.sampled:
        draws[row.draw] = draws.get(row.draw, 0) + 1
        splits[row.split] = splits.get(row.split, 0) + 1
    print(f"\ndraws  {draws}")
    print(f"splits {splits}")
    print(f"\nwrote {evalset.SAMPLE_PATH} and {evalset.STRATA_PATH}")
    print("Commit both - they are ids and provenance only, no content.")


# --- terminal -------------------------------------------------------------


def read_key() -> str:
    """One keypress, no Enter. Falls back to line mode off a tty.

    Two hundred messages is two hundred spurious Enters otherwise. The fallback
    is what lets the loop be driven from a pipe in a test without a pty.

    `setcbreak` leaves ISIG on, so Ctrl-C still raises `KeyboardInterrupt` and
    quits through the same path as `q` - every decision is already on disk.
    """
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        return line.strip()[:1] if line.strip() else "q"
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def footer(unsure: bool) -> str:
    keys = "  ".join(f"{key} {category.value}" for key, category in KEYS.items())
    flag = "  [UNSURE - the next digit is recorded as unsure]" if unsure else ""
    return (
        f"{RULE}\n{keys}\n"
        f"u unsure (flag)    s skip   b back   m more   q save and quit{flag}"
    )


# --- label ----------------------------------------------------------------


def cmd_label(args: argparse.Namespace) -> int:
    rows, seed = evalset.load_sample()
    records, damaged = evallabel.load_records(evallabel.LABELED_PATH)
    if damaged:
        print(f"warning: {damaged} unreadable row(s) in {evallabel.LABELED_PATH}")

    resolved = dict(evallabel.resolve(records))
    order = evallabel.ordering(rows, seed)
    total = len(order)

    if args.relabel:
        # `b` only walks back through the current sitting, so without this
        # there is no way to revise a decision made on a previous day - and a
        # rule refined at message 150 usually needs applying to something
        # labelled at message 20. Appends a correction; last row in file order
        # wins, exactly as `b` does.
        unknown = [m for m in args.relabel if m not in set(order)]
        if unknown:
            print(f"not in the sample: {' '.join(unknown)}", file=sys.stderr)
            return 1
        queue = deque(args.relabel)
        print(f"relabelling {len(queue)} message(s) - appends a correction")
    else:
        queue = deque(evallabel.pending(order, resolved))
        print(f"{len(resolved)}/{total} labelled, {len(queue)} to go  (seed {seed})")
        if not queue:
            print("pass 1 is complete. Run `verify`.")
            return 0

    skipped = label_loop(
        queue,
        resolved,
        pass_no=1,
        total=total,
        body_chars=args.body_chars,
        limit=args.limit,
        batch=args.batch,
    )

    print(f"\n{len(resolved)}/{total} labelled this pass.")
    if skipped:
        print(f"{len(skipped)} skipped this sitting: {' '.join(sorted(skipped))}")
    print(f"{evallabel.LABELED_PATH} is up to date - commit it.")
    return 0


def label_loop(
    queue: deque,
    resolved: dict,
    *,
    pass_no: int,
    total: int,
    body_chars: int,
    limit: int | None,
    batch: int,
) -> set[str]:
    """The keypress loop. Mutates `resolved`, returns the sitting's skips.

    One loop for both passes rather than a copy per subcommand. The copy would
    be the code that writes ground truth, and two of those drift - `b` fixed in
    one and not the other is a silent misfire that survives into the labels.

    `pass_no` is the only difference between a labelling sitting and the blind
    recheck. Nothing here reads a record from another pass, so the pass-1 label
    cannot reach the screen: `render` takes a `Cached`, which does not carry
    one, exactly as it does not carry a stratum.
    """
    # Authenticated lazily: a session that finds everything already cached
    # never touches Gmail at all, which is what makes a resume offline - and
    # what makes the recheck, whose 30 messages are all cached, fully offline.
    service: list = []

    def gmail():
        if not service:
            service.append(gmail_client.service())
        return service[0]

    skipped: set[str] = set()
    history: list[str] = []
    decided = 0

    try:
        while queue and (limit is None or decided < limit):
            prefetch(gmail, queue, batch)
            message_id = queue[0]
            message = evallabel.cache_get(message_id, CACHE_DIR)
            if message is None:
                print(f"{message_id}: could not be fetched - skipping")
                skipped.add(queue.popleft())
                continue

            unsure = False
            show_full = False
            while True:
                print(f"\n{RULE}")
                print(
                    evallabel.render(
                        message,
                        body_chars,
                        show_full,
                        position=len(resolved) + 1,
                        total=total,
                    )
                )
                print(footer(unsure))
                key = read_key()

                if key in KEYS:
                    record = evallabel.LabelRecord(
                        message_id=message_id,
                        label=KEYS[key],
                        unsure=unsure,
                        labelled_at=dt.datetime.now(dt.UTC).isoformat(),
                        pass_no=pass_no,
                    )
                    evallabel.append_record(record, evallabel.LABELED_PATH)
                    resolved[message_id] = record
                    history.append(queue.popleft())
                    decided += 1
                    break
                if key == "u":
                    unsure = not unsure
                    continue
                if key == "m":
                    show_full = True
                    continue
                if key == "s":
                    skipped.add(queue.popleft())
                    break
                if key == "b":
                    if history:
                        queue.appendleft(history.pop())
                    else:
                        print("nothing to go back to")
                    break
                if key == "q":
                    queue.clear()
                    break
    except KeyboardInterrupt:
        print("\ninterrupted")
    return skipped


def prefetch(gmail, queue, batch: int) -> None:
    """Cache the next `batch` messages, if they are not already on disk.

    Fifty sequential gets is ~25 seconds, and `docs/BACKLOG.md` measured the
    rate limit between 150 and 175 consecutive `messages.get` - so a chunk this
    size stays well under it, and the ~25 minutes of human labelling that
    follows resets the window entirely. No throttle for the same reason.

    Caching at fetch rather than at label time is what makes quitting cheap:
    stop after ten and the other forty are already on disk.
    """
    missing = evallabel.cache_missing(list(queue)[:batch], CACHE_DIR)
    if not missing:
        return
    svc = gmail()
    for i, message_id in enumerate(missing, 1):
        print(f"\rfetching {i}/{len(missing)}...", end="", flush=True)
        evallabel.cache_put(gmail_client.fetch(svc, message_id), CACHE_DIR)
    print("\r" + " " * 30 + "\r", end="")


# --- recheck ---------------------------------------------------------------


def cmd_recheck(args: argparse.Namespace) -> int:
    """Pass 2: re-label a blind subset and report self-consistency.

    The ceiling this measures is what the model's accuracy should be read
    against. A labeller who reproduces 27 of 30 of their own decisions has
    built ground truth that no classifier can score above ~0.90, however good
    it is.

    Blind in two senses. The screen never shows the pass-1 label - `render`
    takes a `Cached`, which cannot carry one - and **the comparison prints only
    once the draw is complete**, because learning at message 10 that you have
    already disagreed twice changes how carefully you judge messages 11 to 30.
    """
    rows, _ = evalset.load_sample()
    sample_ids = [row.message_id for row in rows]
    records, damaged = evallabel.load_records(evallabel.LABELED_PATH)
    if damaged:
        print(f"warning: {damaged} unreadable row(s) in {evallabel.LABELED_PATH}")

    incomplete = evallabel.unfinished(records, sample_ids, 1)
    if incomplete:
        print(
            f"pass 1 is not complete - {len(incomplete)} message(s) unlabelled. "
            "A recheck drawn from a partly-labelled set measures the order things "
            "happened to be labelled in as much as the labeller's consistency.",
            file=sys.stderr,
        )
        return 1

    try:
        draw = pin_draw(sample_ids, args)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1

    second = evallabel.resolve(records, evallabel.RECHECK_PASS)
    queue = deque(evallabel.pending(draw.message_ids, second))
    total = len(draw.message_ids)

    if queue and not args.report:
        print(f"recheck: {total} messages, {len(queue)} to go  (seed {draw.seed})")
        print("The pass-1 label is not shown; the comparison prints when the draw is done.")
        label_loop(
            queue,
            second,
            pass_no=evallabel.RECHECK_PASS,
            total=total,
            body_chars=args.body_chars,
            limit=args.limit,
            batch=args.batch,
        )
        # Re-read rather than trusting the in-memory dict: `b` appends a
        # correction, and the file is where last-row-wins is decided.
        records, _ = evallabel.load_records(evallabel.LABELED_PATH)
        second = evallabel.resolve(records, evallabel.RECHECK_PASS)

    done = [message_id for message_id in draw.message_ids if message_id in second]
    if len(done) < total:
        print(f"\n{len(done)}/{total} rechecked. {evallabel.LABELED_PATH} is up to date.")
        print("The report prints when the draw is complete - seeing the agreement rate")
        print("part-way would change how the remaining messages are judged.")
        return 0

    recheck_report(
        draw,
        evallabel.resolve(records, 1),
        {message_id: second[message_id] for message_id in draw.message_ids},
    )
    return 0


def pin_draw(sample_ids, args: argparse.Namespace) -> evallabel.RecheckDraw:
    """The pinned draw: made once, then reused. It grows, but never moves.

    Growing is safe only because `recheck_draw` has the prefix property, so a
    larger `n` at the same seed keeps every id already labelled. Anything else
    - a different seed, a smaller `n`, a sample that has since changed - would
    silently redefine what pass 2 measured, so it is refused rather than
    honoured.
    """
    existing = evallabel.load_draw(evallabel.RECHECK_PATH)
    wanted = tuple(evallabel.recheck_draw(sample_ids, args.n, args.seed))

    if existing is None:
        draw = evallabel.RecheckDraw(
            n=len(wanted),
            seed=args.seed,
            created_at=dt.datetime.now(dt.UTC).isoformat(),
            message_ids=wanted,
        )
        evallabel.save_draw(draw, evallabel.RECHECK_PATH)
        print(f"pinned {len(wanted)} ids -> {evallabel.RECHECK_PATH} - commit it, ids only")
        return draw

    pinned = existing.message_ids
    if existing.seed != args.seed:
        raise ValueError(
            f"{evallabel.RECHECK_PATH} is pinned to seed {existing.seed}, "
            f"--seed says {args.seed}. The draw is the measurement plan; "
            "delete the file only if you mean to start the recheck over."
        )
    if pinned == wanted:
        return existing
    if len(wanted) > len(pinned) and wanted[: len(pinned)] == pinned:
        grown = replace(existing, n=len(wanted), message_ids=wanted)
        evallabel.save_draw(grown, evallabel.RECHECK_PATH)
        print(
            f"extended the draw {len(pinned)} -> {len(wanted)}; "
            f"the first {len(pinned)} are unchanged, so rows already recorded stand"
        )
        return grown
    raise ValueError(
        f"--n {args.n} does not extend the {len(pinned)} ids pinned in "
        f"{evallabel.RECHECK_PATH}. A draw only ever grows: a smaller n, or a "
        "sample that has changed since, would move what pass 2 measured."
    )


def recheck_report(
    draw: evallabel.RecheckDraw, first: dict, second: dict
) -> None:
    """Two ceilings, their intervals, and every disagreement.

    Two rather than one because the harness reports two headline numbers: the
    6-way rate bounds `accuracy`, and the keeps-INBOX rate bounds the collapsed
    action accuracy, which is the metric with consequences. Wilson comes from
    `evalscore` rather than being written again here - one estimator, one
    implementation.
    """
    result = evallabel.agreement(first, second)
    print(f"\n{RULE}")
    print(
        f"blind recheck  {result.n} of {len(first)}  "
        f"seed {draw.seed}  drawn {draw.created_at[:10]}"
    )

    print(f"\n{'':<14}{'agree':>9}{'rate':>8}{'95% CI':>16}")
    for name, agreed in (
        ("6-way", result.n_agree),
        ("keeps-INBOX", result.n_action_agree),
    ):
        low, high = evalscore.wilson(agreed, result.n)
        rate = agreed / result.n if result.n else 0.0
        print(
            f"{name:<14}{f'{agreed}/{result.n}':>9}{rate:>8.3f}"
            f"{f'[{low:.2f}, {high:.2f}]':>16}"
        )
    print("\n6-way bounds accuracy; keeps-INBOX bounds action accuracy. Read the")
    print("model's numbers against these, not against 1.0 - and against the interval,")
    print("which at this n is wide enough to matter.")

    print(
        f"\nunsure in either pass: {result.n_unsure}/{result.n}, "
        f"of which {result.n_unsure_agree} agreed"
    )

    if not result.disagreements:
        print("\nno disagreements.")
        return

    print(f"\n{len(result.disagreements)} disagreement(s), pass 1 -> pass 2:")
    for item in result.disagreements:
        flags = "  [unsure]" if item.unsure else ""
        flags += "  [CROSSES KEEPS_INBOX]" if item.crosses_inbox else ""
        print(f"  {item.message_id}  {item.first} -> {item.second}{flags}")
        message = evallabel.cache_get(item.message_id, CACHE_DIR)
        if message:
            print(f"      from: {message.sender}")
            print(f"      subj: {message.subject}")
    print("\nSender and subject are terminal-only and reach no committed file.")
    print("Where pass 2 found a genuine pass-1 error, fix it with `label --relabel <id>`")
    print("and re-run `run_eval.py score` - scoring is free, no re-inference. The")
    print("ceiling above is measured BEFORE those fixes and is not re-measured here.")


# --- verify ---------------------------------------------------------------


def cmd_verify(args: argparse.Namespace) -> int:
    rows, seed = evalset.load_sample()
    records, damaged = evallabel.load_records(evallabel.LABELED_PATH)
    resolved = evallabel.resolve(records, args.pass_no)
    sample_ids = [row.message_id for row in rows]

    # Pass 1 is checked against the whole sample; pass 2 against the pinned
    # draw. Checking the recheck against all 200 would report the 170 messages
    # it was never meant to visit as missing, and bury the ones that are.
    expected, where = sample_ids, "sample"
    draw = None
    if args.pass_no != 1:
        try:
            draw = evallabel.load_draw(evallabel.RECHECK_PATH)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
        where = "draw"
        expected = list(draw.message_ids) if draw else sorted(resolved)

    missing = evallabel.unfinished(records, expected, args.pass_no)
    stray = sorted(set(resolved) - set(expected))
    uncached = [m for m in resolved if evallabel.cache_get(m, CACHE_DIR) is None]

    print(f"sample   {len(sample_ids)} messages, seed {seed}")
    print(f"labelled {len(resolved)} in pass {args.pass_no}")
    if args.pass_no != 1:
        if draw:
            print(f"draw     {len(expected)} messages, seed {draw.seed}")
        else:
            print(
                f"draw     none - no {evallabel.RECHECK_PATH}, so completeness "
                "cannot be checked. Run `recheck` to pin one."
            )

    counts = evallabel.counts_by_stratum(rows, resolved)
    n_h = {s["name"]: s["N_h"] for s in strata()["strata"]}
    # `w_h` is a pass-1 quantity: `run_eval` weights the labelled set, and it
    # reads pass 1. For pass 2 the same column would be a weight nothing uses,
    # so the table becomes a plain count of which strata the draw reached.
    weighted = args.pass_no == 1
    got = "labelled" if weighted else "rechecked"
    header = f"{'stratum':<12}{'N_h':>8}{'sampled':>9}{got:>10}{'w_h' if weighted else '':>9}"
    print(f"\n{header.rstrip()}")
    for name in sorted(counts):
        cell = counts[name]
        weight = f"{n_h.get(name, 0) / cell['labelled']:.1f}" if cell["labelled"] else "-"
        line = (
            f"{name:<12}{n_h.get(name, 0):>8}{cell['sampled']:>9}"
            f"{cell['labelled']:>10}{weight if weighted else '':>9}"
        )
        print(line.rstrip())
    if weighted:
        print("w_h is derived from the LABELLED count - that is what run_eval weights by.")
    else:
        print("Which strata the draw reached. No w_h: run_eval weights pass 1, not this.")

    distribution = Counter(record.label.value for record in resolved.values())
    print(f"\nlabels {dict(distribution.most_common())}")
    unsure = [m for m, record in resolved.items() if record.unsure]
    print(f"unsure {len(unsure)}")

    problems = 0
    if damaged:
        print(f"\nPROBLEM: {damaged} unreadable row(s) in {evallabel.LABELED_PATH}")
        problems += 1
    if stray:
        print(f"\nPROBLEM: {len(stray)} labelled id(s) not in the {where}:")
        print(id_list(stray))
        problems += 1
    if uncached:
        print(f"\nPROBLEM: {len(uncached)} labelled id(s) missing from the cache:")
        print(id_list(uncached))
        problems += 1
    if missing:
        print(f"\nINCOMPLETE: {len(missing)} message(s) unlabelled in pass "
              f"{args.pass_no}:")
        print(id_list(missing))
        command = "label" if args.pass_no == 1 else "recheck"
        print(f"  Run `{command}` again. An email that cannot be labelled at all is")
        print("  a taxonomy finding, not a skip - see docs/PHASE2_PLAN.md.")
        problems += 1
    elif args.pass_no == 1:
        print("\npass 1 is complete - the blind recheck (pass 2) may start.")

    if not problems:
        print("\nOK")
    return 1 if problems else 0


def id_list(ids, cap: int = 12) -> str:
    """Ids, capped. A complete dump of 200 buries the line that explains them."""
    shown = sorted(ids)[:cap]
    more = len(ids) - len(shown)
    return "  " + " ".join(shown) + (f"  ... and {more} more" if more else "")


def strata() -> dict:
    """`strata.json`, for `N_h`. Missing file means a sample never drawn."""
    path = Path(evalset.STRATA_PATH)
    if not path.exists():
        return {"strata": []}
    return json.loads(path.read_text(encoding="utf-8"))


# --- entry point ----------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sample = sub.add_parser("sample", help="fix the eval sample")
    sample.add_argument("--seed", type=int, default=evalset.DEFAULT_SEED)
    sample.add_argument(
        "--end",
        type=dt.date.fromisoformat,
        default=None,
        help=(
            "last day of the sampling window, YYYY-MM-DD (default: today). "
            "Pinned into strata.json, so re-running with the same value "
            "re-enumerates the same population."
        ),
    )
    sample.add_argument(
        "--window-years", type=int, default=evalset.FRAME_WINDOW_YEARS
    )
    sample.add_argument(
        "--refresh",
        action="store_true",
        help="re-enumerate the frame instead of using the cached listing",
    )
    sample.set_defaults(func=cmd_sample)

    label = sub.add_parser(
        "label",
        help="hand-label the sample (resumable)",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    label.add_argument(
        "--limit", type=int, default=None, help="stop after this many decisions"
    )
    label.add_argument(
        "--body-chars",
        type=int,
        default=1500,
        help=(
            "how much body to show before the truncation marker. Informational "
            "only - ground truth is what the email IS, not what the model sees."
        ),
    )
    label.add_argument(
        "--batch", type=int, default=50, help="messages fetched per Gmail chunk"
    )
    label.add_argument(
        "--relabel",
        nargs="+",
        metavar="MESSAGE_ID",
        default=None,
        help=(
            "revise these already-labelled messages instead of continuing. "
            "Appends a correction; the last row in file order wins. `b` only "
            "reaches back through the current sitting, so this is how a rule "
            "refined late gets applied to something labelled early."
        ),
    )
    label.set_defaults(func=cmd_label)

    verify = sub.add_parser("verify", help="check the labelled set is whole")
    verify.add_argument("--pass", dest="pass_no", type=int, default=1)
    verify.set_defaults(func=cmd_verify)

    recheck = sub.add_parser(
        "recheck",
        help="blind pass 2 over a subset: the self-consistency ceiling",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    recheck.add_argument(
        "--n",
        type=int,
        default=evallabel.DEFAULT_RECHECK_N,
        help=(
            "how many of the sample to re-present. The draw is pinned to "
            "eval/recheck.json on first use; a larger n later extends it, "
            "keeping every id already rechecked (default: %(default)s)"
        ),
    )
    recheck.add_argument(
        "--seed",
        type=int,
        default=evallabel.DEFAULT_RECHECK_SEED,
        help="draw seed, its own, distinct from the sample's (default: %(default)s)",
    )
    recheck.add_argument(
        "--body-chars",
        type=int,
        default=1500,
        help=(
            "as `label`, and it should match what pass 1 used: a different "
            "value measures a different view of the message, not the labeller"
        ),
    )
    recheck.add_argument("--limit", type=int, default=None)
    recheck.add_argument("--batch", type=int, default=50)
    recheck.add_argument(
        "--report",
        action="store_true",
        help="print the comparison for a finished draw without labelling anything",
    )
    recheck.set_defaults(func=cmd_recheck)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
