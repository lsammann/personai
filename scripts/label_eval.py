#!/usr/bin/env python3
"""Build and label the Phase 2 eval set.

Only `sample` exists so far - it fixes which 200 messages get hand-labelled,
and writes nothing but ids and provenance. `label`, `verify` and `recheck`
follow in step 3.

Sampling and labelling are separate subcommands on purpose. The frame is then
fixed and auditable, and resuming a labelling session is `sampled - labelled`
rather than a re-derived query that might have drifted since.

Read-only: list calls only, and not a single `messages.get`. Nothing here can
touch a label.

    uv run python scripts/label_eval.py sample --seed 7
    uv run python scripts/label_eval.py sample --refresh    # re-enumerate
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from app import evalset, gmail_client


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
