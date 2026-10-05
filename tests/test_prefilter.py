"""Sender-domain matching, and a guard on the committed allowlist file."""

import json

import pytest

from app import prefilter
from app.config import PREFILTER_PATH

ALLOWLIST = frozenset({"uniqlo.co.uk", "linkedin.com"})


@pytest.mark.parametrize(
    "sender",
    [
        "offers@uniqlo.co.uk",
        "Uniqlo <offers@uniqlo.co.uk>",
        '"Uniqlo UK" <offers@uniqlo.co.uk>',
        "OFFERS@UNIQLO.CO.UK",
    ],
)
def test_exact_domain_matches(sender):
    assert prefilter.matches(sender, ALLOWLIST)


@pytest.mark.parametrize(
    "sender",
    ["news@email.uniqlo.co.uk", "x@mail.marketing.linkedin.com"],
)
def test_subdomain_matches(sender):
    """Bulk senders almost always mail from a subdomain, so exact-match-only
    would miss most of what the list is aimed at."""
    assert prefilter.matches(sender, ALLOWLIST)


@pytest.mark.parametrize(
    "sender",
    [
        "x@evil-uniqlo.co.uk",     # the dot boundary is what stops this
        "x@uniqlo.co.uk.attacker.com",
        "x@notlinkedin.com",
        "x@gmail.com",
    ],
)
def test_near_misses_do_not_match(sender):
    assert not prefilter.matches(sender, ALLOWLIST)


@pytest.mark.parametrize("sender", ["", "not an address", "no-at-sign", "@", None])
def test_malformed_senders_have_no_domain(sender):
    assert prefilter.sender_domain(sender) is None
    assert not prefilter.matches(sender or "", ALLOWLIST)


def test_sender_domain_lowercases():
    assert prefilter.sender_domain("A@Example.COM") == "example.com"


def test_empty_allowlist_matches_nothing():
    assert not prefilter.matches("offers@uniqlo.co.uk", frozenset())


def test_missing_file_degrades_to_an_empty_allowlist(tmp_path):
    """The prefilter is an optimisation; a missing file should slow the run
    down, not stop it."""
    assert prefilter.load_allowlist(tmp_path / "nope.json") == frozenset()


def test_load_normalises_case_and_whitespace(tmp_path):
    path = tmp_path / "domains.json"
    path.write_text(json.dumps(["  Uniqlo.CO.UK ", "linkedin.com", "  "]))
    assert prefilter.load_allowlist(path) == {"uniqlo.co.uk", "linkedin.com"}


def test_committed_allowlist_is_valid():
    """Mirrors test_config's example-file guard: the committed list is
    hand-edited, and a bad entry silently matches nothing rather than erroring."""
    entries = json.loads(PREFILTER_PATH.read_text())
    assert isinstance(entries, list)
    for entry in entries:
        assert isinstance(entry, str)
        assert entry == entry.strip().lower()
        assert "@" not in entry
        assert "/" not in entry
        assert not entry.startswith(".")
        assert "." in entry


# --- the reply rule -------------------------------------------------------


@pytest.mark.parametrize(
    "subject",
    ["Re: hello", "RE: HELLO", "re: x", "re : x", "  Re: padded",
     "Fw: x", "FW: x", "Fwd: x", "FWD: x", "Re:Re: nested"],
)
def test_a_reply_or_forward_is_recognised(subject):
    assert prefilter.is_reply(subject) is True


@pytest.mark.parametrize(
    "subject",
    ["Reminder: your trial ends", "Receipt: order 123", "Result: your test",
     "Renewal due", "Review your purchase", "re-engage with us",
     "Your Re: something", "", "   ", None],
)
def test_what_merely_starts_with_re_is_not_a_reply(subject):
    """The colon is load-bearing.

    "Reminder:" and "Receipt:" both open with "re", and both are exactly the
    promotional and transactional mail the rule is supposed to leave alone. A
    matcher without the colon would keep a large slice of the archive pile in
    the inbox permanently.
    """
    assert prefilter.is_reply(subject) is False


def test_the_reply_rule_reads_only_the_subject():
    """No body, no sender, no fetch beyond metadata - and nothing that can
    fail on malformed content."""
    import inspect

    assert list(inspect.signature(prefilter.is_reply).parameters) == ["subject"]
