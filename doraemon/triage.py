"""Triage: skip marketing before the model runs (spec step 1).

Gmail already sorts mail into tabs. On the labeled set, every promo was in
Promotions and no real item was, so its label is trusted, with a safety net:
anything that reads like an order, booking or bill still goes through.
"""
import re

from doraemon.email_parse import ParsedEmail

SKIP_CATEGORIES = {"promotions", "social", "forums"}
_ADVERT_RE = re.compile(r"<ADV>|\[ADV\]|\(ADV\)", re.IGNORECASE)
# Real mail that Gmail sometimes files under Promotions
_TRANSACTIONAL_RE = re.compile(
    r"\b(order|receipt|invoice|booking|reservation|itinerary|e-?ticket|payment|paid|"
    r"shipped|delivered|delivery|due|statement|bill|appointment|refund)\b",
    re.IGNORECASE,
)


def skip_reason(email: ParsedEmail) -> str | None:
    """Why this email doesn't need the model, or None if it does."""
    if _ADVERT_RE.search(email.subject):
        return "advertisement tag in subject"
    skipped = email.gmail_categories & SKIP_CATEGORIES
    if skipped and not _TRANSACTIONAL_RE.search(email.subject):
        return f"Gmail {sorted(skipped)[0]} tab"
    return None
