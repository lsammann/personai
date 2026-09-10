"""Which text does the model read? Pure policy, no I/O.

This is deliberately *not* in `gmail_client`. Three things were bundled there
in the first design and only one of them talks to Gmail:

  fetching, base64 decoding, walking the MIME tree   -> gmail_client
  turning HTML into readable text                    -> here (generic)
  choosing WHICH text the model reads                -> here (policy)

The third is the reason the split matters. Preferring HTML over a stub plain
part is a decision about model input, exactly like `body_chars` - if
`STUB_MAX_CHARS` moves from 200 to 300, every prediction changes and two eval
runs stop being comparable. A tuning knob living inside the transport adapter
would be invisible to the harness that is supposed to control for it, which is
why `EXTRACTION_VERSION` below is recorded beside `model`, `prompt_id` and
`body_chars` on every run.

Measured basis for all of it, over 150 messages from the Phase 2 eval frame
(`scripts/measure_bodies.py`, written up in `docs/BACKLOG.md` -> Body
structure): 76.7% of mail has a real `text/plain` part, 23.3% is HTML-only,
and 1.3% carries a stub plain part with the real content only in the HTML.
Stripping tags keeps 3.2% of the HTML source.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import ClassVar, Literal

# Bump whenever anything changes what text comes out: the constants below, the
# tag-skip set, the selection branches - or, upstream, which MIME parts
# `gmail_client` collects. v2 appends an attached
# message's text after the covering note rather than merging or dropping it. Recorded in the
# eval run manifest and on every log row, for the same reason `prompt_id` is -
# a silent change here makes runs either side of it incomparable, and nothing
# about the output would look wrong.
EXTRACTION_VERSION = "v2"

# A `text/plain` part shorter than this, sitting beside substantially more
# stripped HTML, is a "View this email in your browser" stub rather than a
# body. 200 characters is roughly that line plus an unsubscribe footer.
STUB_MAX_CHARS = 200

# ...and "substantially more" means this multiple of the plain part's length.
# The ratio is what protects genuinely short human mail: a two-line note whose
# HTML part says the same two lines fails the test and keeps its plain text,
# where a bare length threshold would have thrown it away.
STUB_HTML_RATIO = 2.0

# Where the returned text came from. Recorded per message so Phase 2 can ask
# whether accuracy differs on HTML-only mail - a question that matters at 23%
# of the mailbox and is unanswerable after the fact.
BodySource = Literal["plain", "html", "stub_fallback", "none"]


@dataclass(frozen=True)
class Selection:
    """The chosen body text, and enough about the choice to segment on it."""

    text: str
    source: BodySource
    # False when the HTML parser aborted partway. The text is what was parsed
    # before the failure, so it is short rather than absent - which would
    # otherwise be indistinguishable from a genuinely short email and would
    # quietly drag the measured body-length distribution down.
    parsed_ok: bool


class _TextExtractor(HTMLParser):
    """Tags out, entities decoded, whitespace collapsed. Nothing else.

    Stdlib rather than html2text or BeautifulSoup. The output is truncated to
    ~1500 characters and read by a classifier looking for topic, so everything
    a real library buys over this - table layout, link reference lists,
    heading structure, encoding repair - is discarded by the truncation
    anyway. What is actually needed is tags gone, script/style CONTENT gone,
    and entities decoded, and HTMLParser does all three in fifteen lines with
    no dependency to justify.
    """

    # Dropping the CONTENT of these, not merely their tags: a stylesheet or a
    # tracking script inlined in the head is thousands of characters of
    # `{font-size:14px}` that would otherwise be indistinguishable from body
    # text once the angle brackets are gone.
    SKIP: ClassVar[frozenset[str]] = frozenset({"script", "style", "head", "title"})

    # Tags that do NOT imply a word boundary. Everything else does.
    #
    # `<p>Hello</p><p>world</p>` arrives as two adjacent data chunks with no
    # whitespace between them, so without a separator they concatenate into
    # "Helloworld" - and on real mail, into "Amount due87.42". The inverse
    # error matters too: emphasis inside a word, `un<b>believable</b>`, must
    # not become two words. Hence a small inline set rather than a space at
    # every tag, or none.
    INLINE: ClassVar[frozenset[str]] = frozenset(
        {"a", "b", "i", "u", "em", "strong", "span", "small", "sub", "sup",
         "font", "code", "abbr", "mark", "s", "strike"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag not in self.INLINE:
            self._chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        # Guarded: malformed mail closes tags it never opened, and an
        # unguarded decrement would go negative and un-skip a real <style>.
        if tag in self.SKIP:
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag not in self.INLINE:
            self._chunks.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        # Collapse all whitespace: markup-derived text arrives full of
        # newlines and indentation that carry no meaning and would spend the
        # truncation budget on nothing.
        return " ".join("".join(self._chunks).split())


def strip_html(html: str) -> tuple[str, bool]:
    """HTML -> readable text, plus whether the parse completed.

    Malformed markup must not stop a classification, but it must not be silent
    either - a half-parsed body is shorter than the real one, and silence
    would make it look like a short email. The flag travels on `Selection` so
    the caller can record it.
    """
    if not html.strip():
        return "", True
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - any parse error, classification continues
        return parser.text(), False
    return parser.text(), True


def select_body(text_plain: str, text_html: str) -> Selection:
    """Choose the text the model reads. Prefer plain; fall back to HTML.

    The stub case is the subtle one. 1.3% of mail carries a `text/plain` part
    that is only "View this email in your browser" while the whole message
    lives in the HTML - and it fails silently, because the extractor succeeds
    and returns something that looks exactly like a short email. Without this
    branch those messages reach the model as a sender, a subject and twenty
    characters of nothing.

    Plain text keeps its internal whitespace where HTML does not: its line
    breaks are the author's and carry structure (an amount and a due date on
    their own lines), whereas markup-derived whitespace is an artifact of
    indentation. Whether collapsing plain text too would buy back useful
    truncation budget is a Phase 2 question, not a guess to make here.
    """
    plain = text_plain.strip()
    html_text, parsed_ok = strip_html(text_html)

    if html_text and (not plain or _is_stub(plain, html_text)):
        return Selection(
            text=html_text,
            source="stub_fallback" if plain else "html",
            parsed_ok=parsed_ok,
        )
    return Selection(
        text=plain, source="plain" if plain else "none", parsed_ok=parsed_ok
    )


def _is_stub(plain: str, html_text: str) -> bool:
    """A short plain part beside substantially more HTML is a stub, not a body."""
    return (
        len(plain) < STUB_MAX_CHARS
        and len(html_text) > STUB_HTML_RATIO * len(plain)
    )
