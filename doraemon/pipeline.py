"""One email through the whole pipeline: triage and rules before the model, the model, rules after."""
from doraemon.config import Settings
from doraemon.email_parse import ParsedEmail
from doraemon.extract import extract
from doraemon.llm import Backend
from doraemon.rules import RuleStore
from doraemon.schema import Extraction
from doraemon.triage import skip_reason


def should_skip(email: ParsedEmail, rules: RuleStore) -> str | None:
    if rule := rules.ignored_by(email.sender):
        return f"ignored sender ({rule})"
    return skip_reason(email)


def process(
    email: ParsedEmail,
    backend: Backend,
    settings: Settings,
    rules: RuleStore,
    raw: Extraction | None = None,
) -> Extraction:
    """`raw` is a model result from an earlier run, to try rule changes without re-running the model."""
    if reason := should_skip(email, rules):
        return Extraction(message_id=email.message_id, skipped=reason)
    if raw is None:
        raw = extract(email, backend, settings)
    return rules.apply(raw.model_copy(deep=True), sender=email.sender)
