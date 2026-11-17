"""The keypress loop that writes ground truth, shared by every labeller.

Moved out of `scripts/label_eval.py` in Phase 3 step 9 (decision 1), so the
eval labeller and the dry-run review (`scripts/review_run.py`) run the same
loop. A copy per script is what the loop's own docstring warns against:
`b` fixed in one and not the other is a silent misfire that survives into
the labels.

Everything specific to one caller is passed in as a function: where a
message comes from (`fetch`, `prefetch`), what is printed above the keys
(`show`) and how a decision is stored (`record`). The loop itself knows
the keys and nothing else. `read` is a parameter so tests can drive it with
a scripted sequence of keys.

Terminal I/O lives here (`read_key`, printing). Nothing here touches Gmail:
`prefetch` is handed the function that fetches one message.
"""

from __future__ import annotations

import sys
import termios
import tty
from collections import deque
from collections.abc import Callable
from pathlib import Path

from app import evallabel
from app.categories import Category
from app.evallabel import KEYS, Cached, LabelRecord
from app.gmail_client import Message

RULE = "-" * 78


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


def label_loop(
    queue: deque[str],
    resolved: dict[str, LabelRecord],
    *,
    fetch: Callable[[str], Cached | None],
    show: Callable[[Cached, bool, int], str],
    record: Callable[[str, Category, bool], LabelRecord],
    prefetch: Callable[[deque[str]], None],
    limit: int | None,
    read: Callable[[], str] = read_key,
) -> set[str]:
    """The keypress loop. Mutates `queue` and `resolved`, returns the sitting's skips.

    - `fetch(message_id)` reads one message, None if it could not be fetched
    - `show(message, show_full, position)` is the text printed above the keys
    - `record(message_id, category, unsure)` stores one decision and returns it
    - `prefetch(queue)` is called before each message, to fill the cache ahead

    Every decision is stored by `record` before the next message is shown, so
    `q` and Ctrl-C lose nothing. `b` puts the last decision of this sitting
    back at the front; the re-label is appended and wins by file order
    (`evallabel.resolve`).

    Nothing here reads a record back from storage, and `show` takes only a
    `Cached`, so a label from another pass cannot reach the screen unless the
    caller's `show` puts it there.
    """
    skipped: set[str] = set()
    history: list[str] = []
    decided = 0

    try:
        while queue and (limit is None or decided < limit):
            prefetch(queue)
            message_id = queue[0]
            message = fetch(message_id)
            if message is None:
                print(f"{message_id}: could not be fetched - skipping")
                skipped.add(queue.popleft())
                continue

            unsure = False
            show_full = False
            while True:
                print(f"\n{RULE}")
                print(show(message, show_full, len(resolved) + 1))
                print(footer(unsure))
                key = read()

                if key in KEYS:
                    resolved[message_id] = record(message_id, KEYS[key], unsure)
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


def prefetch(
    get: Callable[[str], Message | None],
    queue: deque[str],
    batch: int,
    directory: Path,
) -> None:
    """Cache the next `batch` messages in `directory`, if not already there.

    `get(message_id)` fetches one message; callers pass `gmail_client.fetch`
    bound to a lazily built service, so a sitting whose messages are all
    cached never touches Gmail. A `get` may return None for a message it
    could not fetch: nothing is cached, so `label_loop` skips it when it
    reaches the front. Moved here with the loop (step 9) so the review does
    not copy it.

    Fifty sequential gets is ~25 seconds, and `docs/BACKLOG.md` measured the
    rate limit between 150 and 175 consecutive `messages.get` - so a chunk this
    size stays well under it, and the ~25 minutes of human labelling that
    follows resets the window entirely. No throttle for the same reason.

    Caching at fetch rather than at label time is what makes quitting cheap:
    stop after ten and the other forty are already on disk.
    """
    missing = evallabel.cache_missing(list(queue)[:batch], directory)
    for i, message_id in enumerate(missing, 1):
        print(f"\rfetching {i}/{len(missing)}...", end="", flush=True)
        message = get(message_id)
        if message is not None:
            evallabel.cache_put(message, directory)
    if missing:
        print("\r" + " " * 30 + "\r", end="")

