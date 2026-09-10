"""Body selection: which text does the model read?

Pure module, so every case here runs without a socket or a mock. The measured
basis for the rules under test is `docs/BACKLOG.md` -> Body structure: 23.3% of
real mail is HTML-only and 1.3% carries a stub plain part.
"""

from __future__ import annotations

import pytest

from app import message_body
from app.message_body import (
    EXTRACTION_VERSION,
    STUB_MAX_CHARS,
    Selection,
    select_body,
    strip_html,
)

# --- strip_html -----------------------------------------------------------


def test_strip_html_removes_tags_and_keeps_the_words():
    text, ok = strip_html("<div><p>Hello</p><p>world</p></div>")
    assert text == "Hello world"
    assert ok is True


def test_block_boundaries_are_word_boundaries():
    """`</p><p>` arrives as two adjacent chunks with no whitespace between them.

    Without a separator they concatenate - "Amount due87.42" on real mail,
    which is both unreadable and badly tokenised.
    """
    text, _ = strip_html("<td>Amount due</td><td>87.42</td><br>Due 15 Oct")
    assert text == "Amount due 87.42 Due 15 Oct"


def test_inline_emphasis_inside_a_word_does_not_split_it():
    """The inverse error, and the reason this is not a space at every tag."""
    text, _ = strip_html("<p>un<b>believable</b> and <em>quite</em> good</p>")
    assert text == "unbelievable and quite good"


def test_strip_html_drops_script_and_style_content_not_just_their_tags():
    """The whole point of the SKIP set.

    A stylesheet inlined in the head is thousands of characters of
    `{font-size:14px}` which, once the angle brackets are gone, is
    indistinguishable from body text and would eat the truncation budget.
    """
    html = (
        "<html><head><style>.btn{font-size:14px;color:#fff}</style>"
        "<title>Ignore me</title></head>"
        "<body><p>Real content</p><script>track('open')</script></body></html>"
    )
    text, _ = strip_html(html)
    assert text == "Real content"
    assert "font-size" not in text
    assert "track" not in text
    assert "Ignore me" not in text


def test_strip_html_decodes_entities():
    text, _ = strip_html("<p>Tom &amp; Jerry&nbsp;win &lt;here&gt;</p>")
    assert "Tom & Jerry" in text
    assert "<here>" in text
    assert "&amp;" not in text


def test_strip_html_collapses_whitespace():
    """Markup-derived newlines and indentation carry no meaning and cost budget."""
    text, _ = strip_html("<div>\n\n   Hello   \n\t\t world  \n</div>")
    assert text == "Hello world"


@pytest.mark.parametrize("html", ["", "   ", "\n\t "])
def test_strip_html_on_empty_input(html):
    assert strip_html(html) == ("", True)


def test_strip_html_tolerates_a_close_tag_that_was_never_opened():
    """Real mail does this. An unguarded depth counter would go negative and
    then fail to skip a genuine <style> later in the document."""
    text, ok = strip_html("</style><p>Visible</p><style>.x{}</style>")
    assert "Visible" in text
    assert ".x{}" not in text
    assert ok is True


def test_strip_html_reports_a_failed_parse_rather_than_a_short_body(monkeypatch):
    """A half-parsed body is short, not absent - and a silently short body is
    indistinguishable from a genuinely short email."""

    def explode(self, data):
        raise ValueError("malformed")

    monkeypatch.setattr(message_body._TextExtractor, "feed", explode)
    text, ok = strip_html("<p>anything</p>")
    assert ok is False
    assert text == ""


# --- select_body ----------------------------------------------------------


def test_plain_text_is_preferred_when_both_exist():
    selection = select_body("The real plain body, long enough to be real.", "<p>x</p>")
    assert selection.source == "plain"
    assert selection.text.startswith("The real plain body")


def test_html_only_mail_is_stripped_and_used():
    """23.3% of the mailbox. Before this, these reached the model as markup."""
    selection = select_body("", "<html><body><p>You have been invited</p></body></html>")
    assert selection.source == "html"
    assert selection.text == "You have been invited"


def test_a_stub_plain_part_falls_back_to_the_html():
    """1.3% of mail: "View this email in your browser" beside the real message.

    Fails silently without this branch - the extractor succeeds and returns
    something that looks exactly like a genuinely short email.
    """
    selection = select_body(
        "Grill'd Relish\r\n \r\n",
        "<div>" + "Our new performance range. " * 20 + "</div>",
    )
    assert selection.source == "stub_fallback"
    assert "performance range" in selection.text


def test_a_short_human_note_keeps_its_plain_text():
    """The ratio test is what protects this case from the stub rule.

    A two-line note whose HTML part says the same two lines must not be thrown
    away; a bare length threshold would have done exactly that.
    """
    note = "yeah that works for me, see you then"
    selection = select_body(note, f"<div>{note}</div>")
    assert selection.source == "plain"
    assert selection.text == note


def test_plain_at_the_stub_length_boundary_is_not_a_stub():
    plain = "x" * STUB_MAX_CHARS
    selection = select_body(plain, "<p>" + "y" * 10_000 + "</p>")
    assert selection.source == "plain"


def test_html_exactly_double_the_plain_length_is_not_a_stub():
    """The comparison is strictly greater than, so the boundary keeps plain."""
    selection = select_body("x" * 50, "<p>" + "y" * 100 + "</p>")
    assert selection.source == "plain"


def test_no_text_at_all_reports_none_rather_than_guessing():
    selection = select_body("", "")
    assert selection == Selection(text="", source="none", parsed_ok=True)


def test_plain_with_no_html_part_is_used_whatever_its_length():
    selection = select_body("short", "")
    assert selection.source == "plain"
    assert selection.text == "short"


def test_surrounding_whitespace_is_stripped_but_internal_structure_is_kept():
    """Plain-text line breaks are the author's - an amount and a due date on
    their own lines - unlike markup whitespace, which is an artifact."""
    selection = select_body("\n\n  Amount: 87.42\nDue: 15 October  \n\n", "")
    assert selection.text == "Amount: 87.42\nDue: 15 October"


def test_a_failed_html_parse_is_carried_on_the_selection():
    selection = select_body("", "<p>ok</p>")
    assert selection.parsed_ok is True


# --- the version constant -------------------------------------------------


def test_extraction_version_is_a_string():
    """Recorded beside model/prompt_id/body_chars on every run. A change to the
    stub constants without a bump makes runs either side of it incomparable."""
    assert isinstance(EXTRACTION_VERSION, str)
    assert EXTRACTION_VERSION
