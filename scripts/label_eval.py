#!/usr/bin/env python3
"""Build and label the Phase 2 eval set.

    label_eval.py sample --seed 7      fix which 200 messages get labelled
    label_eval.py label                hand-label them, resumable
    label_eval.py verify               check the set is whole and consistent

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
from pathlib import Path

from app import evallabel, evalset, gmail_client
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

    # Authenticated lazily: a session that finds everything already cached
    # never touches Gmail at all, which is what makes a resume offline.
    service: list = []

    def gmail():
        if not service:
            service.append(gmail_client.service())
        return service[0]

    skipped: set[str] = set()
    history: list[str] = []
    decided = 0

    try:
        while queue and (args.limit is None or decided < args.limit):
            prefetch(gmail, queue, args.batch)
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
                        args.body_chars,
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

    print(f"\n{len(resolved)}/{total} labelled this pass.")
    if skipped:
        print(f"{len(skipped)} skipped this sitting: {' '.join(sorted(skipped))}")
    print(f"{evallabel.LABELED_PATH} is up to date - commit it.")
    return 0


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


# --- verify ---------------------------------------------------------------


def cmd_verify(args: argparse.Namespace) -> int:
    rows, seed = evalset.load_sample()
    records, damaged = evallabel.load_records(evallabel.LABELED_PATH)
    resolved = evallabel.resolve(records, args.pass_no)
    sample_ids = [row.message_id for row in rows]

    missing = evallabel.unfinished(records, sample_ids, args.pass_no)
    stray = sorted(set(resolved) - set(sample_ids))
    uncached = [m for m in resolved if evallabel.cache_get(m, CACHE_DIR) is None]

    print(f"sample   {len(sample_ids)} messages, seed {seed}")
    print(f"labelled {len(resolved)} in pass {args.pass_no}")

    counts = evallabel.counts_by_stratum(rows, resolved)
    n_h = {s["name"]: s["N_h"] for s in strata()["strata"]}
    print(f"\n{'stratum':<12}{'N_h':>8}{'sampled':>9}{'labelled':>10}{'w_h':>9}")
    for name in sorted(counts):
        cell = counts[name]
        weight = f"{n_h.get(name, 0) / cell['labelled']:.1f}" if cell["labelled"] else "-"
        print(
            f"{name:<12}{n_h.get(name, 0):>8}{cell['sampled']:>9}"
            f"{cell['labelled']:>10}{weight:>9}"
        )
    print("w_h is derived from the LABELLED count - that is what run_eval weights by.")

    distribution = Counter(record.label.value for record in resolved.values())
    print(f"\nlabels {dict(distribution.most_common())}")
    unsure = [m for m, record in resolved.items() if record.unsure]
    print(f"unsure {len(unsure)}")

    problems = 0
    if damaged:
        print(f"\nPROBLEM: {damaged} unreadable row(s) in {evallabel.LABELED_PATH}")
        problems += 1
    if stray:
        print(f"\nPROBLEM: {len(stray)} labelled id(s) not in the sample:")
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
        print("  Run `label` again. An email that cannot be labelled at all is a")
        print("  taxonomy finding, not a skip - see docs/PHASE2_PLAN.md.")
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

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
