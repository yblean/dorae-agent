"""Rules store: corrections remembered as exact, repeatable fixes.

A rule says "when X, do Y" and is applied in plain code, so it gives the same
answer every time. Rules come from two places:
- defaults.toml, shipped with the app (SG merchants, airport timezones)
- the user_rules table in SQLite, written by the user's corrections

User rules win over defaults. The model handles everything no rule covers.
"""
import re
import sqlite3
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from doraemon.schema import Category, Extraction, ItemType

DEFAULTS_PATH = Path(__file__).with_name("defaults.toml")
RULE_TYPES = ("merchant_category", "ignore_sender", "travel_timezone", "not_spending")
_NOISE = {"singapore", "sg", "pte", "ltd", "the", "co", "inc", "llc", "sdn", "bhd", "com", "www"}


def name_key(name: str) -> str:
    """Core words of a merchant or place name: 'McDonalds 930255 Singapore SG' -> 'mcdonalds'."""
    text = name.lower().replace("'", "").replace("’", "")
    text = text.replace("singapore", " singapore ")  # bank alerts glue it on: "Pte LSINGAPORE"
    words = re.findall(r"[a-z]+", text)  # letters only, so store and branch numbers drop out
    return " ".join(w for w in words if len(w) > 1 and w not in _NOISE)


def name_matches(rule_key: str, name: str) -> bool:
    words = set(name_key(name).split())
    return all(w in words for w in rule_key.split())


def _sender_matches(rule_match: str, sender: str) -> bool:
    address = parseaddr(sender)[1].lower()
    if "@" in rule_match:
        return address == rule_match
    domain = address.rpartition("@")[2]
    return domain == rule_match or domain.endswith("." + rule_match)


@dataclass(frozen=True)
class Rule:
    rule_type: str
    match: str
    value: str
    source: str = "default"  # "default" or "correction"
    created_from: str | None = None  # the email whose correction created it
    id: int | None = None

    def __str__(self) -> str:
        origin = f"#{self.id} from {self.created_from}" if self.source == "correction" else "default"
        return f"{self.rule_type}: {self.match!r} -> {self.value!r} ({origin})"


def _normalize(rule_type: str, match: str, value: str) -> tuple[str, str]:
    if rule_type not in RULE_TYPES:
        raise ValueError(f"unknown rule type {rule_type!r}; expected one of {RULE_TYPES}")
    if rule_type == "ignore_sender":
        match = parseaddr(match)[1].lower() or match.strip().lower()
    else:
        match = name_key(match)
    if not match:
        raise ValueError("rule has nothing to match on")
    if rule_type == "merchant_category":
        value = Category(value).value  # raises on unknown categories
    elif rule_type == "travel_timezone":
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone {value!r}") from None
    return match, value


def load_defaults(path: Path = DEFAULTS_PATH) -> list[Rule]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    rules = []
    for rule_type in ("merchant_category", "travel_timezone"):
        for match, value in data.get(rule_type, {}).items():
            rules.append(Rule(rule_type, *_normalize(rule_type, match, value)))
    for sender in data.get("ignore_sender", {}).get("senders", []):
        rules.append(Rule("ignore_sender", *_normalize("ignore_sender", sender, "")))
    for payee in data.get("not_spending", {}).get("payees", []):
        rules.append(Rule("not_spending", *_normalize("not_spending", payee, "")))
    return rules


class RuleStore:
    def __init__(self, db_path: str | Path = ":memory:", use_defaults: bool = True) -> None:
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # Web requests run on worker threads; ingest may be writing at the same time
        self.db = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS user_rules (
                id INTEGER PRIMARY KEY,
                rule_type TEXT NOT NULL,
                match TEXT NOT NULL,
                value TEXT NOT NULL,
                created_from_correction TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (rule_type, match)
            )""")
        self.db.commit()
        self.defaults = load_defaults() if use_defaults else []

    # --- managing rules -------------------------------------------------------

    def add(self, rule_type: str, match: str, value: str = "", created_from: str | None = None) -> Rule:
        """Save a rule from a correction. A newer rule for the same match replaces the old one."""
        match, value = _normalize(rule_type, match, value)
        self.db.execute(
            """INSERT INTO user_rules (rule_type, match, value, created_from_correction, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (rule_type, match) DO UPDATE SET
                 value = excluded.value,
                 created_from_correction = excluded.created_from_correction,
                 created_at = excluded.created_at""",
            (rule_type, match, value, created_from, datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()
        return next(r for r in self.user_rules() if r.rule_type == rule_type and r.match == match)

    def delete(self, rule_id: int) -> bool:
        cur = self.db.execute("DELETE FROM user_rules WHERE id = ?", (rule_id,))
        self.db.commit()
        return cur.rowcount > 0

    def user_rules(self) -> list[Rule]:
        rows = self.db.execute(
            "SELECT id, rule_type, match, value, created_from_correction FROM user_rules ORDER BY id"
        )
        return [Rule(t, m, v, "correction", src, id_) for id_, t, m, v, src in rows]

    def all_rules(self) -> list[Rule]:
        return self.user_rules() + self.defaults

    def _find(self, rule_type: str, name: str | None) -> Rule | None:
        if not name:
            return None
        matching = [r for r in self.all_rules() if r.rule_type == rule_type and name_matches(r.match, name)]
        # Your corrections beat defaults; then the most specific (most words) wins.
        matching.sort(key=lambda r: (r.source != "correction", -len(r.match.split())))
        return matching[0] if matching else None

    # --- applying rules -------------------------------------------------------

    def ignored_by(self, sender: str) -> Rule | None:
        """Checked BEFORE the model: an ignored sender's email is skipped entirely."""
        return next(
            (r for r in self.all_rules() if r.rule_type == "ignore_sender" and _sender_matches(r.match, sender)),
            None,
        )

    def apply(self, extraction: Extraction, sender: str = "") -> Extraction:
        """Applied AFTER the model: rules override what the model returned. Mutates and returns it.

        `sender` backs up merchant matching: a Shopee receipt often names only the seller
        ("dollarsaver"), but the email comes from "Shopee".
        """
        log = extraction.applied_rules
        sender_name = parseaddr(sender)[0]
        for txn in list(extraction.transactions):
            if rule := self._find("not_spending", txn.merchant):
                extraction.transactions.remove(txn)
                log.append(f"not spending: dropped {txn.merchant!r} {txn.amount} ({rule})")
                continue
            rule = self._find("merchant_category", txn.merchant) or self._find("merchant_category", sender_name)
            if rule and txn.category != rule.value:
                log.append(f"category: {txn.merchant!r} {txn.category} -> {rule.value} ({rule})")
                txn.category = Category(rule.value)

        for item in extraction.items:
            if item.type not in (ItemType.FLIGHT, ItemType.OTHER_TRAVEL):
                continue
            rule = self._find("travel_timezone", item.departs_from)
            if rule and item.timezone != rule.value:
                # The printed time is the local departure time: keep the clock, change the zone.
                zone = ZoneInfo(rule.value)
                log.append(f"timezone: {item.title!r} {item.timezone} -> {rule.value} ({rule})")
                item.start_at = item.start_at.replace(tzinfo=zone) if item.start_at else None
                item.end_at = item.end_at.replace(tzinfo=zone) if item.end_at else None
                item.timezone = rule.value
        return extraction
