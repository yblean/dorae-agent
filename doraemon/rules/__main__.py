"""See and edit your rules until the review page exists.

    python -m doraemon.rules list
    python -m doraemon.rules add merchant_category "Kopitiam" dining
    python -m doraemon.rules add ignore_sender news@shop.example
    python -m doraemon.rules add travel_timezone "Changi Airport" Asia/Singapore
    python -m doraemon.rules add not_spending "LEAN MUN SOON"
    python -m doraemon.rules delete 3
"""
import argparse

from doraemon.config import Settings
from doraemon.rules import RULE_TYPES, RuleStore


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m doraemon.rules")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--defaults", action="store_true", help="also show built-in rules")
    add = sub.add_parser("add")
    add.add_argument("rule_type", choices=RULE_TYPES)
    add.add_argument("match")
    add.add_argument("value", nargs="?", default="")
    rm = sub.add_parser("delete")
    rm.add_argument("id", type=int)
    args = ap.parse_args()

    store = RuleStore(Settings().db_path)
    if args.cmd == "list":
        rules = store.all_rules() if args.defaults else store.user_rules()
        print("\n".join(map(str, rules)) or "No rules yet. Add --defaults to see the built-in ones.")
    elif args.cmd == "add":
        print("saved", store.add(args.rule_type, args.match, args.value, created_from="manual"))
    elif args.cmd == "delete":
        print("deleted" if store.delete(args.id) else f"no rule #{args.id}")


if __name__ == "__main__":
    main()
