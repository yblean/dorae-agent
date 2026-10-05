"""Stand-in for the review page: turn label-vs-prediction differences into rules.

In the app, a rule is saved when the user edits a card. In the eval, the label
plays the user: after each email is scored, its corrections become rules that
apply to later emails only.
"""
from doraemon.email_parse import ParsedEmail
from doraemon.rules import RuleStore
from doraemon.schema import Extraction, TxnSource
from evals.labels import Label


def learn(store: RuleStore, name: str, email: ParsedEmail, pred: Extraction, label: Label) -> list[str]:
    learned = []

    def save(rule_type: str, match: str, value: str = "") -> None:
        try:
            learned.append(str(store.add(rule_type, match, value, created_from=name)))
        except ValueError:
            pass  # nothing usable to match on

    # "Dismiss: this is marketing" -> never process this sender again
    if label.notes == "promo" and (pred.items or pred.transactions):
        save("ignore_sender", email.sender)
        return learned

    # Category edits on a transaction the model found
    unused = list(pred.transactions)
    for want in label.transactions:
        got = next((t for t in unused if t.amount == want.amount and t.is_refund == want.is_refund), None)
        if got is None:
            continue
        unused.remove(got)
        if got.category != want.category:
            save("merchant_category", got.merchant, want.category.value)

    # "Not spending" on a bank alert in an email that has no real spending (e.g. a transfer from family)
    if not label.transactions:
        for extra in unused:
            if extra.source == TxnSource.BANK_ALERT:
                save("not_spending", extra.merchant)
    return learned
