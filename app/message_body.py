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

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import ClassVar, Literal
from urllib.parse import urlsplit

# Bump whenever anything changes what text comes out: the constants below, the
# tag-skip set, the selection branches - or, upstream, which MIME parts
# `gmail_client` collects. v2 appends an attached message's text after the
# covering note rather than merging or dropping it. v3 measures the stub test
# over informative characters, honours an explicit "HTML only" declaration, and
# rewrites URLs to their host. v4 strips a `text/plain` part that is really an
# HTML document, and adds U+034F to the spacer set. Recorded in the eval run
# manifest and on every log row, for the same reason `prompt_id` is - a silent
# change here makes runs either side of it incomparable, and nothing about the
# output would look wrong.
#
# v3 and v4 both landed on 2026-09-12 and no run used v3. It was still bumped
# rather than folded in: the version is what a future reader maps a result file
# onto, and one label meaning two different rules is the exact failure it
# exists to prevent.
EXTRACTION_VERSION = "v4"

# A `text/plain` part shorter than this, sitting beside substantially more
# stripped HTML, is a "View this email in your browser" stub rather than a
# body. 200 characters is roughly that line plus an unsubscribe footer.
STUB_MAX_CHARS = 200

# ...and "substantially more" means this multiple of the plain part's length.
# The ratio is what protects genuinely short human mail: a two-line note whose
# HTML part says the same two lines fails the test and keeps its plain text,
# where a bare length threshold would have thrown it away.
STUB_HTML_RATIO = 2.0

# Phrases by which a plain part declares itself a placeholder. These waive the
# LENGTH test only - the ratio guard above still has to pass - so a real email
# quoting one of them cannot be thrown away unless the HTML genuinely carries
# substantially more.
#
# This exists because length alone provably cannot catch the case. Measured on
# a SEEK newsletter (docs/BACKLOG.md -> Stub detection misses a whole class of
# it): 707 plain characters, of which 361 were two tracking URLs, against 1,061
# stripped HTML characters, of which 104 were zero-width spacers. Both halves
# of the old rule failed - too long AND a ratio of 1.5 - so no value of
# STUB_MAX_CHARS would have recovered it while the lengths were being measured
# over padding.
STUB_MARKERS = (
    "only available in html",
    "view this email in your browser",
    "view it in your browser",
    "having trouble viewing",
)

# Zero-width characters. Bulk senders emit runs of these as "preheader
# spacers", to control what a mail client shows in its preview line. They are
# invisible, carry no meaning under any policy, and spent 10% of that SEEK
# message's truncation budget.
# U+034F leads by a distance - 3,081 occurrences across 64 cached messages
# against 603 for U+200C - and was missed on the first pass because the SEEK
# message that prompted it happened to use the more obvious one. Measured, not
# enumerated from a Unicode chart: characters are added here when they are seen
# in real mail.
ZERO_WIDTH = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad\u034f")
)

# A `text/plain` part whose first characters declare an HTML document. Some
# senders ship the same markup under both MIME types, and preferring plain then
# feeds the model 1,500 characters of `<meta name="viewport">` - the precise
# failure DESIGN.md records for the old extractor, returning through a
# mis-declared content type rather than a missing part. 1 of 64 cached
# messages.
#
# Matched on the declaration rather than on tag density. A density test would
# also catch markup pasted into a genuine plain-text email, which is a
# different thing and would be the wrong call; anything that opens with a
# doctype is a document, not a quotation.
MARKUP_PREFIXES = ("<!doctype html", "<html")

# Deliberately greedy to the next whitespace: a tracking URL is one unbroken
# token, and over-matching trailing punctuation costs nothing once only the
# host survives.
URL = re.compile(r"https?://\S+")

# Where the returned text came from. Recorded per message so Phase 2 can ask
# whether accuracy differs on HTML-only mail - a question that matters at 23%
# of the mailbox and is unanswerable after the fact.
BodySource = Literal["plain", "plain_markup", "html", "stub_fallback", "none"]


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
        # truncation budget on nothing. Zero-width spacers go the same way and
        # for the same reason - they are whitespace that does not even render.
        return " ".join(strip_zero_width("".join(self._chunks)).split())


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


def strip_zero_width(text: str) -> str:
    """Remove invisible spacer characters. See `ZERO_WIDTH`."""
    return text.translate(ZERO_WIDTH)


def shorten_urls(text: str) -> str:
    """Rewrite every URL down to its host.

    Measured over the eval cache (docs/BACKLOG.md -> Tracking URLs are eating
    the truncation budget): URLs are a median 28% of a body and over a quarter
    of the visible budget on 10 of 22 messages, with one message carrying 252
    of them and one single URL running to 1,495 characters. In tokens the
    share is worse still - a base64 tracking parameter is close to the worst
    case for a BPE tokenizer - and on CPU-only inference the prompt length *is*
    the latency.

    The host survives rather than the whole URL being deleted, which costs
    about five points of the available saving and buys back the only part that
    carries signal: `account.proton.me`, `click.discord.com` and
    `click.e.afl.com.au` all say something about what kind of mail this is. A
    `?qs=ABB7InYiOjEs...` tracking token cannot, which is why this change did
    not wait for a measurement to justify it.
    """
    return URL.sub(lambda match: urlsplit(match.group(0)).netloc or "link", text)


def informative_length(text: str) -> int:
    """How many characters actually say something.

    URLs and zero-width padding removed, whitespace collapsed. Both stub tests
    measure through this rather than over `len()`, which is the correction that
    makes them mean what they claim: `len(plain) < 200` should be "200
    characters of prose", not "200 characters, one of which may be a tracking
    token". Note this normalisation decides *which part is selected* - it is
    not what the model reads, which is `shorten_urls` above.
    """
    return len(" ".join(URL.sub(" ", strip_zero_width(text)).split()))


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
    plain, plain_source, plain_ok = _read_plain(text_plain)
    html_text, html_ok = strip_html(text_html)

    if html_text and (not plain or _is_stub(plain, html_text)):
        text, source = html_text, "stub_fallback" if plain else "html"
    else:
        text, source = plain, plain_source if plain else "none"
    return Selection(
        text=shorten_urls(text), source=source, parsed_ok=plain_ok and html_ok
    )


def _read_plain(text_plain: str) -> tuple[str, BodySource, bool]:
    """The `text/plain` part as words - stripping it first if it is markup.

    Reported as `plain_markup` rather than `plain` so the cohort stays visible.
    It is small (1 of 64 cached messages) but it is exactly the kind of thing
    that is unanswerable after the fact, and `body_source` is already the field
    the eval segments accuracy by.
    """
    plain = strip_zero_width(text_plain).strip()
    if plain[:200].lower().startswith(MARKUP_PREFIXES):
        stripped, ok = strip_html(plain)
        return stripped, "plain_markup", ok
    return plain, "plain", True


def _is_stub(plain: str, html_text: str) -> bool:
    """Is this plain part a placeholder rather than a body?

    The ratio guard comes first and applies to both routes in: whatever the
    plain part says about itself, the HTML has to carry substantially more
    before it can displace it. That is what keeps a genuinely short human note
    - whose HTML part says the same two lines - out of this branch entirely.

    Past that guard there are two ways to qualify. Short is the original test,
    now measured over informative characters. Declaring itself is the new one:
    "this email is only available in HTML" is the sender stating outright that
    the plain part is a placeholder, and no length threshold can be set low
    enough to catch that message while still being high enough to catch the
    same declaration wrapped in a footer and two tracking URLs.

    `STUB_MAX_CHARS` is deliberately unchanged at 200. Whether it should move
    is the one part of this rule that needs a distribution rather than an
    argument, and the eval cache is the instrument for it - see
    docs/BACKLOG.md -> Worth re-measuring later.
    """
    plain_len = informative_length(plain)
    if informative_length(html_text) <= STUB_HTML_RATIO * plain_len:
        return False
    return plain_len < STUB_MAX_CHARS or _declares_html_only(plain)


def _declares_html_only(plain: str) -> bool:
    lowered = plain.lower()
    return any(marker in lowered for marker in STUB_MARKERS)
