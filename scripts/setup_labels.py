#!/usr/bin/env python3
"""Create the nine `Agent/` labels and their `Agent` parent. Phase 3 steps 2-3.

    setup_labels.py            print what would be created; create nothing
    setup_labels.py --apply    create what is missing

Create-only. It never renames and never deletes. A case-variant clash
(`agent/to action` already present) stops it before anything is created,
even with `--apply`: renaming the reader's label is their call, not this
script's.

Idempotent: a second `--apply` finds all ten present and creates nothing.

Needs `gmail.labels` and `gmail.readonly` as GRANTED, not as requested.
Labels alone would do for this script, but every later step needs both, and
Google's consent screen can grant them separately, so a partial grant is
caught here.
"""

from __future__ import annotations

import argparse
import sys

from app import auth, gmail_client
from app.categories import SETUP_LABELS, plan_label_setup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply", action="store_true", help="create the missing labels"
    )
    args = parser.parse_args()

    auth.require_scopes(auth.LABELS, auth.READONLY)
    svc = gmail_client.service()

    plan = plan_label_setup(gmail_client.all_labels(svc))

    if plan.clashes:
        print("Case-variant clash - nothing created. Resolve by hand in Gmail:")
        for wanted, existing in plan.clashes:
            print(f"  want {wanted!r}, found {existing!r}")
        return 1

    if not args.apply:
        for name in plan.present:
            print(f"  present       {name}")
        for name in plan.to_create:
            print(f"  would create  {name}")
        print(f"\n{len(plan.to_create)} to create. Rerun with --apply to create them.")
        return 0

    created = set()
    for name in plan.to_create:
        gmail_client.create_label(svc, name)
        created.add(name)

    # Re-read through the same lookup every later step uses, so success here
    # means those steps will find every label.
    ids = gmail_client.label_ids(svc, sorted(SETUP_LABELS))
    for name, label_id in ids.items():
        state = "created" if name in created else "already present"
        print(f"  {name:<22} {label_id:<24} {state}")
    print(f"\n{len(created)} created, {len(ids) - len(created)} already present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
