"""Compare one Extraction to its Label and add up the spec's metrics."""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from doraemon.ledger import build_ledger
from doraemon.schema import ActionItem, Extraction, ItemType, Transaction
from evals.labels import Label, LabelItem


def date_matches(label: LabelItem, pred: ActionItem, tz: str) -> bool:
    if label.start_at is None:
        return pred.start_at is None
    if pred.start_at is None:
        return False
    if len(label.start_at) == 10:  # date only
        return pred.start_at.date().isoformat() == label.start_at
    want = datetime.fromisoformat(label.start_at)
    if want.tzinfo is None:
        want = want.replace(tzinfo=ZoneInfo(tz))
    return abs((pred.start_at - want).total_seconds()) < 60


def amount_matches(label: LabelItem, pred: ActionItem) -> bool:
    # Only a bill's amount is the point of the item. A booking's price is checked
    # on its transaction, so a hotel item without a price isn't wrong.
    if label.type != ItemType.BILL or label.amount is None:
        return True
    return pred.amount == label.amount


@dataclass
class Score:
    emails: int = 0
    label_items: int = 0
    pred_items: int = 0
    matched_items: int = 0
    wrong_fields: int = 0  # matched items with a wrong date/time or amount
    label_txns: int = 0
    pred_txns: int = 0
    matched_txns: int = 0
    right_category: int = 0
    actionable_emails: int = 0
    missed_actionable_emails: int = 0
    latency_s: float = 0.0
    failures: list[str] = field(default_factory=list)
    all_pred_txns: list[Transaction] = field(default_factory=list)
    all_label_txns: list[Transaction] = field(default_factory=list)

    def add(self, name: str, pred: Extraction, label: Label, tz: str) -> None:
        self.emails += 1
        self.all_pred_txns += pred.transactions
        self.all_label_txns += [t.to_transaction(name, tz) for t in label.transactions]
        self.latency_s += pred.latency_s
        self.label_items += len(label.items)
        self.pred_items += len(pred.items)

        if label.items:
            self.actionable_emails += 1
            if not pred.items:
                self.missed_actionable_emails += 1

        # Match each labeled item to an unused prediction of the same type,
        # preferring one with the right date.
        unused = list(pred.items)
        for want in label.items:
            same_type = [p for p in unused if p.type == want.type]
            if not same_type:
                self.failures.append(f"{name}: MISSED {want.type} {want.title!r}")
                continue
            got = next((p for p in same_type if date_matches(want, p, tz)), same_type[0])
            unused.remove(got)
            self.matched_items += 1
            if not (date_matches(want, got, tz) and amount_matches(want, got)):
                self.wrong_fields += 1
                self.failures.append(
                    f"{name}: WRONG {want.type} want {want.start_at} {want.amount}, "
                    f"got {got.start_at} {got.amount} (from {got.date_text!r})"
                )
        for extra in unused:
            self.failures.append(f"{name}: EXTRA {extra.type} {extra.title!r}")

        self.label_txns += len(label.transactions)
        self.pred_txns += len(pred.transactions)
        unused_t = list(pred.transactions)
        for want in label.transactions:
            got = next((t for t in unused_t if t.amount == want.amount and t.is_refund == want.is_refund), None)
            if got is None:
                self.failures.append(f"{name}: MISSED transaction {want.merchant!r} {want.amount}")
                continue
            unused_t.remove(got)
            self.matched_txns += 1
            if got.source != want.source:
                self.failures.append(f"{name}: SOURCE {got.merchant!r} want {want.source}, got {got.source}")
            if got.category == want.category:
                self.right_category += 1
            else:
                self.failures.append(f"{name}: CATEGORY {got.merchant!r} want {want.category}, got {got.category}")
        for extra in unused_t:
            self.failures.append(f"{name}: EXTRA transaction {extra.merchant!r} {extra.amount}")

        for problem in pred.problems:
            self.failures.append(f"{name}: PROBLEM {problem}")

    def ledger(self) -> dict[str, str]:
        """Run the dedupe over every email and compare the final spending ledgers."""
        want, _ = build_ledger(self.all_label_txns)
        got, dropped = build_ledger(self.all_pred_txns)
        unused = list(got)
        missing = []
        for w in want:
            match = next((g for g in unused if g.amount == w.amount and g.is_refund == w.is_refund
                          and abs((g.purchased_at.date() - w.purchased_at.date()).days) <= 1), None)
            if match:
                unused.remove(match)
            else:
                missing.append(w)
        for w in missing:
            self.failures.append(f"LEDGER missing {w.merchant!r} {w.amount} on {w.purchased_at.date()} ({w.message_id})")
        for g in unused:
            self.failures.append(f"LEDGER extra {g.merchant!r} {g.amount} on {g.purchased_at.date()} (duplicate or false)")
        for d in dropped:
            self.failures.append(f"LEDGER dropped {d.dropped.merchant!r} {d.dropped.amount} as same payment as {d.kept.merchant!r}")

        def total(txns: list[Transaction]) -> Decimal:
            return sum((-t.amount if t.is_refund else t.amount for t in txns), Decimal("0"))

        return {
            "3. Ledger extra entries (target 0)": str(len(unused)),
            "   Ledger missing entries": str(len(missing)),
            "   Total spend, labeled vs predicted": f"{total(want)} vs {total(got)}",
        }

    def summary(self) -> dict[str, str]:
        def pct(n: int, d: int) -> str:
            return f"{100 * n / d:.1f}% ({n}/{d})" if d else "n/a"

        return {
            "1. Item recall (target 98%+)": pct(self.matched_items, self.label_items),
            "   Actionable emails caught": pct(self.actionable_emails - self.missed_actionable_emails, self.actionable_emails),
            "2. Wrong date/time/amount (target <2%)": pct(self.wrong_fields, self.matched_items),
            "4. Category accuracy (target 90%+)": pct(self.right_category, self.matched_txns),
            "   Transaction recall": pct(self.matched_txns, self.label_txns),
            "5. Item precision (target 85%+)": pct(self.matched_items, self.pred_items),
            "6. Avg latency per email": f"{self.latency_s / self.emails:.1f}s" if self.emails else "n/a",
        }
