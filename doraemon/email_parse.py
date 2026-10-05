"""Turn a raw .eml into the plain text the extractor sees."""
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from pypdf import PdfReader

_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "td"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return "".join(parser.parts)


_URL_RE = re.compile(r"<?https?://[^\s>]+>?|\[cid:[^\]]*\]")


def _tidy(text: str) -> str:
    # Tracking links and inline-image markers are most of the tokens in many emails
    # and carry nothing the extractor needs.
    text = _URL_RE.sub("", text)
    # Zero-width characters some senders put inside numbers (e.g. Trip.com booking numbers)
    text = re.sub(r"[​-‍⁠﻿͏­]", "", text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


MAX_PDF_CHARS = 6_000
# Where a reply's quoted copy of the earlier message starts (Outlook, Gmail, classic clients)
_QUOTE_START_RE = re.compile(
    r"^_{10,}\s*\n\s*From:|^-{3,}\s*Original Message\s*-{3,}|^On .{5,200}wrote:\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def strip_quoted_reply(subject: str, body: str) -> str:
    """Drop the quoted earlier message from a reply, so only what's new gets extracted.

    Forwards are left alone: the forwarded message is the content.
    """
    if not re.match(r"\s*(re|aw|回复)\s*:", subject, re.IGNORECASE):
        return body
    match = _QUOTE_START_RE.search(body)
    if match:
        body = body[: match.start()]
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith(">"))


def _pdf_texts(msg) -> list[str]:
    """Text of each PDF attachment. Many airlines and billers put everything in the PDF."""
    texts = []
    for part in msg.walk():
        name = part.get_filename() or ""
        if part.get_content_type() != "application/pdf" and not name.lower().endswith(".pdf"):
            continue
        try:
            reader = PdfReader(io.BytesIO(part.get_payload(decode=True)))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:  # encrypted or broken PDFs shouldn't sink the whole email
            continue
        if text.strip():
            texts.append(f"[Attachment: {name or 'document.pdf'}]\n{text[:MAX_PDF_CHARS]}")
    return texts


@dataclass
class ParsedEmail:
    message_id: str
    subject: str
    sender: str
    sent_at: datetime
    body: str
    gmail_categories: frozenset[str] = frozenset()  # Gmail tabs: promotions, updates, purchases...


def gmail_categories(labels: list[str]) -> frozenset[str]:
    """Gmail tab names from API label ids ('CATEGORY_PROMOTIONS') or Takeout ('Category promotions')."""
    found = set()
    for label in labels:
        label = label.strip().lower()
        for prefix in ("category_", "category "):
            if label.startswith(prefix):
                found.add(label[len(prefix):])
    return frozenset(found)


def parse_eml(raw: bytes) -> ParsedEmail:
    msg = BytesParser(policy=policy.default).parsebytes(raw)

    plain = msg.get_body(preferencelist=("plain",))
    html = msg.get_body(preferencelist=("html",))
    body = plain.get_content() if plain is not None else ""
    # Some senders ship a stub plain part ("undefined", "view in browser") next to the real HTML.
    if html is not None and len(body.strip()) < 200:
        html_text = html_to_text(html.get_content())
        if len(html_text.strip()) > len(body.strip()):
            body = html_text

    subject = str(msg["Subject"] or "")
    body = strip_quoted_reply(subject, body)
    body = "\n\n".join([body, *_pdf_texts(msg)])

    try:
        sent_at = parsedate_to_datetime(msg["Date"])
    except (TypeError, ValueError):
        sent_at = datetime.now(timezone.utc)
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)

    message_id = (msg["Message-ID"] or "").strip() or hashlib.sha256(raw).hexdigest()[:32]

    return ParsedEmail(
        message_id=message_id,
        subject=subject,
        sender=str(msg["From"] or ""),
        sent_at=sent_at,
        body=_tidy(body),
        # Takeout exports carry Gmail's labels in this header; the API passes them separately.
        gmail_categories=gmail_categories(str(msg["X-Gmail-Labels"] or "").split(",")),
    )
