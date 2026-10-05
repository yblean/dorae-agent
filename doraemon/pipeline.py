"""One email through the whole pipeline: triage and rules before the model, the model, rules after."""
from doraemon.config import Settings
from doraemon.email_parse import ParsedEmail, forwarded_original_recipients
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
    result = rules.apply(raw.model_copy(deep=True), sender=email.sender)

    # A forwarded receipt is your spending only if the original was sent to you:
    # a friend forwarding their own booking keeps the trip, not the payment.
    recipients = forwarded_original_recipients(email.subject, email.body)
    if recipients and settings.user_emails and not set(recipients) & set(settings.user_emails):
        for txn in result.transactions:
            result.applied_rules.append(
                f"not your purchase: forwarded booking was for {', '.join(recipients)} "
                f"({txn.merchant} {txn.currency} {txn.amount})"
            )
        result.transactions = []
    return result
