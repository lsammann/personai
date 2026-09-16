"""Deterministic routing: decisions that can be made without the model.

Two rules, pointing in opposite directions. The sender-domain allowlist
archives bulk mail; the reply rule keeps thread mail visible. Both answer
the same question - can this be decided without an LLM call? - and both
are checked before any model call is made.

A match routes straight to Agent/Promotions with INBOX removed and no LLM call,
which is the difference between string matching in microseconds and ~10 seconds
of local inference. The list starts short and grows as repeat senders are
noticed; docs/BACKLOG.md measures it as covering perhaps a third of the
backlog, not most of it.

Prefilter hits are written to the classification log the same as model results,
tagged `source: prefilter`, so the metrics account for them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from email.utils import parseaddr
from pathlib import Path

from app.config import PREFILTER_PATH


def load_allowlist(path: Path | None = None) -> frozenset[str]:
    """Read the committed domain list. A missing file is not an error.

    Degrading to "everything goes to the model" is the right failure mode here:
    the prefilter is an optimisation, and a fresh clone or a renamed file
    should slow the run down, not stop it.
    """
    path = PREFILTER_PATH if path is None else path
    if not path.exists():
        return frozenset()
    entries = json.loads(path.read_text())
    return frozenset(entry.strip().lower() for entry in entries if entry.strip())


def sender_domain(sender: str) -> str | None:
    """`"Uniqlo" <offers@uniqlo.co.uk>` -> `uniqlo.co.uk`.

    `parseaddr` rather than a split on "<": real From headers carry display
    names, quoting and occasional comments, and getting this wrong means the
    allowlist silently stops matching rather than raising.
    """
    _, address = parseaddr(sender or "")
    _, at_sign, domain = address.rpartition("@")
    # rpartition puts the WHOLE string in the third slot when the separator
    # is absent, so a sender with no "@" would otherwise come back as its
    # own domain and match nothing forever without ever erroring.
    if not at_sign:
        return None
    return domain.strip().lower() or None


def matches(sender: str, allowlist: Collection[str]) -> bool:
    """True if the sender's domain is allowlisted, or is a subdomain of one.

    Suffix matching on a dot boundary, because bulk senders almost always mail
    from `email.brand.com` or `mail.brand.com` rather than the bare domain -
    an exact-match-only list would miss most of what it is aimed at. The dot
    boundary is what stops `evil-brand.com` matching an entry of `brand.com`.
    """
    domain = sender_domain(sender)
    if domain is None:
        return False
    return any(
        domain == entry or domain.endswith(f".{entry}") for entry in allowlist
    )


# --- the reply rule -------------------------------------------------------

# A subject that opens "Re:", "Fw:" or "Fwd:" means the message sits in a
# thread the reader is part of. The colon is required: without it "Reminder:"
# and "Receipt:" both begin with "re" and the rule would archive-proof a large
# slice of the promotional mail it is meant to be orthogonal to.
REPLY = re.compile(r"^\s*(re|fw|fwd)\s*:", re.IGNORECASE)


def is_reply(subject: str | None) -> bool:
    """True if this message is a reply or a forward, judged on the subject.

    **The discriminator is "am I in this thread", not "did a human type this".**
    Those come apart on automated replies - a ticketing auto-response, a
    support survey answering a query the reader sent - and the reader's
    judgement is that those are still wanted in the inbox, because they exist
    only because the reader started something. Measured over the 200-message
    eval set, 15 subjects match and all 15 are mail the reader wants visible;
    13 of them are also human-written.

    Deliberately blunt. A stricter version would require the absence of a
    `List-Unsubscribe` header, which is the standard bulk-mail marker - that
    needs a header the message cache does not store, so it is a live-measured
    refinement rather than something this phase can claim. See
    `docs/BACKLOG.md`.

    The subject is the only input, so this is decidable before any fetch cost
    beyond metadata and cannot fail on a malformed body.
    """
    return bool(REPLY.match(subject or ""))
