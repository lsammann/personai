#!/usr/bin/env python3
"""Predict over the eval set, and score what came back.

    run_eval.py predict --model llama3.1:8b --body-chars 300
    run_eval.py score <run_id> --threshold 0.8 --floor 0.15
    run_eval.py compare <run_a> <run_b>

The split is the point. `predict` is slow - one Ollama call per message, ~20
minutes for 200 on this box - and is done once per configuration. `score` is
pure and instant, so thresholds, weighting, dev-vs-holdout and any metric
invented later are free, and re-scoring every historical run costs nothing.

`predict` reads `data/eval_cache/`, never Gmail: an eval run must not depend on
what the mailbox looks like today, or two runs a week apart would be measuring
different populations.

**Lock-box.** `score` reports the 140-message dev split unless `--holdout` is
passed. Pick a winner on dev, then open the box once on that run. Scoring every
historical run on holdout and taking the best just makes it a second dev set.
"""

from __future__ import annotations

import argparse
import json
import sys

from app import evallabel, evalrun, evalscore, evalset, prefilter
from app.categories import Category
from app.classifier import DEFAULT_PROMPT_ID
from app.config import DATA_DIR

CACHE_DIR = DATA_DIR / "eval_cache"
RESULTS_MD = evalset.EVAL_DIR / "RESULTS.md"


def load_ground_truth():
    """Sample, labels, strata and the seed - everything `score` joins against."""
    sample, seed = evalset.load_sample()
    records, damaged = evallabel.load_records(evallabel.LABELED_PATH)
    if damaged:
        print(f"warning: {damaged} unreadable row(s) in {evallabel.LABELED_PATH}")
    strata = json.loads(evalset.STRATA_PATH.read_text(encoding="utf-8"))
    return sample, evallabel.resolve(records), strata, seed


# --- predict --------------------------------------------------------------


def cmd_predict(args: argparse.Namespace) -> int:
    sample, _labels, _strata, seed = load_ground_truth()
    ids = [row.message_id for row in sample]
    if args.limit:
        # The labelling presentation order, so a --limit slice is a
        # deterministic sample mixed across strata rather than whichever ids
        # happen to sort first - which would be an arbitrary date range.
        ids = evallabel.ordering(sample, seed)[: args.limit]

    messages = [evallabel.cache_get(m, CACHE_DIR) for m in ids]
    missing = [m for m, c in zip(ids, messages, strict=True) if c is None]
    if missing:
        print(f"{len(missing)} message(s) not in the cache - run `label_eval.py "
              f"label` first", file=sys.stderr)
        return 1
    messages = [message for message in messages if message is not None]

    orders = evalrun.PERMUTED_ORDERS if args.permutations else evalrun.DEFAULT_ORDERS
    manifest = evalrun.build_manifest(
        model=args.model, body_chars=args.body_chars,
        prompt_id=args.prompt, orders=orders, n_messages=len(messages),
    )
    path = evalrun.result_path(manifest.run_id)
    evalrun.start_result(manifest, path)

    total = len(messages) * len(orders)
    print(f"run {manifest.run_id}")
    print(f"  model={manifest.model}  prompt={manifest.prompt_id} "
          f"({manifest.prompt_hash})  body_chars={manifest.body_chars}")
    print(f"  extraction={manifest.extraction_version}  "
          f"orders={','.join(manifest.orders)}  commit={manifest.git_commit}")
    partial = "" if manifest.n_messages == len(sample) else "  (PARTIAL RUN)"
    print(f"  {manifest.n_messages} messages, {total} calls -> "
          f"{path}{partial}\n")

    seen = [0]

    def progress(row: evalrun.Prediction) -> None:
        seen[0] += 1
        flag = "!" if row.failed else " "
        print(f"\r  {seen[0]}/{total}{flag} {row.latency_ms/1000:5.1f}s",
              end="", flush=True)

    rows = evalrun.predict(
        messages, model=args.model, body_chars=args.body_chars,
        prompt_id=args.prompt, orders=orders, path=path, on_row=progress,
    )
    failures = sum(1 for row in rows if row.failed)
    print(f"\n\ndone. {len(rows)} rows, {failures} failure(s).")
    print(f"score it with:  uv run python scripts/run_eval.py score "
          f"{manifest.run_id}")
    return 0


# --- score ----------------------------------------------------------------


def cmd_score(args: argparse.Namespace) -> int:
    path = evalrun.result_path(args.run_id)
    if not path.exists():
        print(f"no such run: {path}", file=sys.stderr)
        return 1
    result = evalrun.load_result(path)
    sample, labels, strata, _seed = load_ground_truth()

    # Both deterministic rules are computed at score time from the cache, so
    # they cost no inference and every historical run can be re-scored under
    # them. Nothing about a rule needs the model to be re-run.
    senders, subjects = {}, {}
    for row in sample:
        message = evallabel.cache_get(row.message_id, CACHE_DIR)
        if message:
            senders[row.message_id] = message.sender
            subjects[row.message_id] = message.subject

    split = None if args.all else ("holdout" if args.holdout else "dev")
    if args.holdout:
        print("!! OPENING THE LOCK-BOX. The holdout is for detecting inflation,")
        print("!! not for picking a winner. Every opening belongs on the record.\n")

    report = evalscore.score(
        result.predictions, labels, sample, strata,
        confidence_threshold=args.threshold, to_action_floor=args.floor,
        split=split, allowlist=prefilter.load_allowlist(), senders=senders,
        subjects=subjects,
    )
    render(result.manifest, report, args, sample, labels)

    if args.append:
        append_results_row(result.manifest, report, args)
        print(f"\nappended a row to {RESULTS_MD}")
    return 0


def render(manifest, report, args, sample, labels) -> None:
    print(f"run {manifest.run_id}  model={manifest.model}  "
          f"prompt={manifest.prompt_id}  body_chars={manifest.body_chars}")
    print(f"split={'holdout' if args.holdout else 'all' if args.all else 'dev'}"
          f"  T={args.threshold}  F={args.floor}\n")

    low, high = report.accuracy_interval
    print(f"accuracy          {report.accuracy:.3f}  [{low:.3f}, {high:.3f}]  "
          f"n={report.n - report.failures}")
    print(f"weighted (mailbox){report.weighted_accuracy:>7.3f}")
    print(f"excluding unsure  {report.accuracy_excluding_unsure:.3f}  "
          f"({report.n_unsure} unsure rows)")
    if report.failures:
        print(f"failures          {report.failures} (excluded from the above)")

    print("\nTo Action")
    for name, (hit, n) in sorted(report.to_action_recall.items()):
        retained = report.to_action_retention.get(name, (0, 0))
        print(f"  {name:<8} recall {hit}/{n}    retention "
              f"{retained[0]}/{retained[1]}")
    if report.to_action_misses:
        print("  archived anyway:")
        for row in report.to_action_misses:
            message = evallabel.cache_get(row.message_id, CACHE_DIR)
            if message:
                print(f"    {message.sender[:34]:<36}{message.subject[:44]}")

    print("\naction matrix (the one with consequences)")
    for truth in ("keeps", "archives"):
        cells = "  ".join(
            f"{predicted}={report.action_matrix.get((truth, predicted), 0):>3}"
            for predicted in ("keeps", "archives")
        )
        print(f"  truth {truth:<9}{cells}")

    print(f"\ncalibration gap   {report.calibration_gap:+.3f}")
    for row in report.calibration:
        print(f"  [{row['low']:.1f},{row['high']:.1f})  n={row['n']:>3}  "
              f"accuracy {row['accuracy']:.3f}")

    print("\nthreshold sweep")
    print(f"  {'T':>5}{'review':>9}{'auto acc':>10}{'TA missed':>11}")
    for row in report.threshold_sweep:
        print(f"  {row['threshold']:>5}{row['needs_review_rate']:>9.3f}"
              f"{row['auto_accuracy']:>10.3f}{row['to_action_missed']:>11}")

    print(f"\nfloor sweep at T={args.threshold}")
    print(f"  {'F':>5}{'TA kept':>10}{'others archived':>18}")
    for row in report.floor_sweep:
        print(f"  {row['floor']:>5}{row['to_action_kept']:>4}/"
              f"{row['to_action_total']:<5}{row['other_archived']:>18}")

    print("\naccuracy by body_source (n matters - two cohorts are tiny)")
    for source, (hit, n) in report.by_source.items():
        print(f"  {source:<16}{hit}/{n}")

    print(f"\nretained_mass     {report.retained_mass}")
    print(f"latency (ms)      mean {report.latency['mean']:.0f}  "
          f"p50 {report.latency['p50']:.0f}  p90 {report.latency['p90']:.0f}")
    if report.prefilter:
        print(f"prefilter         {report.prefilter} "
              f"(scored on the action, not the 6-way label)")
    if report.reply:
        bucket = report.reply
        print(f"\nreply rule        {bucket['n']} message(s) routed to "
              f"{Category.PERSONAL.value}, INBOX kept")
        print(f"  action correct  rule {bucket['rule_action_correct']}/"
              f"{bucket['n']}   model would have been "
              f"{bucket['model_action_correct']}/{bucket['n']}")
        print(f"  exact label     {bucket['label_exact']}/{bucket['n']} are "
              f"{Category.PERSONAL.value} in ground truth - the gap is the "
              f"widening from \"a human wrote it\" to \"I am in this thread\"")

    stability = evalscore.stability(
        [row for row in evalrun.load_result(
            evalrun.result_path(manifest.run_id)).predictions]
    )
    if stability["n"]:
        print(f"\npermutation       unchanged {stability['unchanged']:.3f}  "
              f"mean TV {stability['mean_tv']:.3f}  n={stability['n']}")


def append_results_row(manifest, report, args) -> None:
    """One row per run, plus the required what-changed note.

    The note is mandatory because a row without one is not a regression
    record - six months from now the numbers alone do not say why the run
    happened.
    """
    RESULTS_MD.parent.mkdir(parents=True, exist_ok=True)
    if not RESULTS_MD.exists():
        RESULTS_MD.write_text(
            "# Eval runs\n\nOne row per prediction run. `score` appends with "
            "`--append --note`.\n\n"
            "| run | model | prompt | body | split | acc | CI | TA recall | "
            "TA retention | gap | note |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|\n",
            encoding="utf-8",
        )
    low, high = report.accuracy_interval
    recall = report.to_action_recall.get("all", (0, 0))
    retention = report.to_action_retention.get("all", (0, 0))
    split = "holdout" if args.holdout else "all" if args.all else "dev"
    with RESULTS_MD.open("a", encoding="utf-8") as handle:
        handle.write(
            f"| {manifest.run_id} | {manifest.model} | {manifest.prompt_id} | "
            f"{manifest.body_chars} | {split} | {report.accuracy:.3f} | "
            f"[{low:.2f},{high:.2f}] | {recall[0]}/{recall[1]} | "
            f"{retention[0]}/{retention[1]} | {report.calibration_gap:+.3f} | "
            f"{args.note} |\n"
        )


# --- compare --------------------------------------------------------------


def cmd_compare(args: argparse.Namespace) -> int:
    sample, labels, _strata, _seed = load_ground_truth()
    runs = []
    for run_id in (args.run_a, args.run_b):
        result = evalrun.load_result(evalrun.result_path(run_id))
        scored = evalscore.join(result.predictions, labels, sample,
                               split=None if args.all else "dev")
        runs.append((result.manifest, {row.message_id: row.correct
                                       for row in scored}))

    (manifest_a, a), (manifest_b, b) = runs
    shared = sorted(set(a) & set(b))
    pairs = [(a[m], b[m]) for m in shared]
    stats = evalscore.mcnemar(pairs)

    print(f"A {manifest_a.run_id}  {manifest_a.model} {manifest_a.prompt_id} "
          f"body={manifest_a.body_chars}")
    print(f"B {manifest_b.run_id}  {manifest_b.model} {manifest_b.prompt_id} "
          f"body={manifest_b.body_chars}")
    print(f"\n{len(shared)} messages in both")
    print(f"  A accuracy  {sum(a[m] for m in shared)/len(shared):.3f}")
    print(f"  B accuracy  {sum(b[m] for m in shared)/len(shared):.3f}")
    print(f"  fixed by B  {stats['n_fixed']}")
    print(f"  broken by B {stats['n_broken']}")
    print(f"  McNemar p   {stats['p_value']:.3f}")
    print("\nOnly the discordant pairs carry information: +2 points from 4 "
          "fixed / 0 broken\nis a real change, +2 from 12 fixed / 8 broken is "
          "noise.")
    return 0


# --- entry point ----------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    predict = sub.add_parser("predict", help="run the model over the eval set")
    predict.add_argument("--model", required=True)
    predict.add_argument("--body-chars", type=int, required=True)
    predict.add_argument("--prompt", default=DEFAULT_PROMPT_ID)
    predict.add_argument(
        "--permutations", action="store_true",
        help="also classify under a rotated and a reversed letter mapping "
             "(triples runtime; for choosing a model, not for prompt tweaks)",
    )
    predict.add_argument("--limit", type=int, default=None)
    predict.set_defaults(func=cmd_predict)

    score = sub.add_parser("score", help="score a finished run (pure, instant)")
    score.add_argument("run_id")
    score.add_argument("--threshold", type=float, default=0.8)
    score.add_argument("--floor", type=float, default=0.15)
    score.add_argument(
        "--holdout", action="store_true",
        help="OPEN THE LOCK-BOX: score the 60 held-out messages instead of dev",
    )
    score.add_argument("--all", action="store_true", help="score both splits")
    score.add_argument("--append", action="store_true",
                       help=f"append a row to {RESULTS_MD}")
    score.add_argument("--note", default="", help="what changed and why")
    score.set_defaults(func=cmd_score)

    compare = sub.add_parser("compare", help="paired comparison of two runs")
    compare.add_argument("run_a")
    compare.add_argument("run_b")
    compare.add_argument("--all", action="store_true")
    compare.set_defaults(func=cmd_compare)

    args = parser.parse_args()
    if getattr(args, "append", False) and not args.note:
        parser.error("--append needs --note: a row with no what-changed line "
                     "is not a regression record")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
