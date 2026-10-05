"""Write a draft label for every email that has none, using the model's guess.

Fixing a draft is faster than labeling from scratch. Open each draft, correct
it against the email, then set "reviewed": true. Unreviewed drafts are not
scored, so the model can't grade its own homework.

    python -m evals.draft_labels
"""
import argparse
from pathlib import Path

from doraemon.config import Settings
from doraemon.email_parse import parse_eml
from doraemon.extract import extract
from doraemon.llm import get_backend
from evals.labels import Label, LabelItem, LabelTransaction


def main() -> None:
    settings = Settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=settings.model)
    ap.add_argument("--data", type=Path, default=Path("data"))
    args = ap.parse_args()

    backend = get_backend(args.model, settings)
    labels_dir = args.data / "labels"
    labels_dir.mkdir(parents=True, exist_ok=True)

    for path in sorted((args.data / "emails").glob("*.eml")):
        label_path = labels_dir / f"{path.stem}.json"
        if label_path.exists():
            continue
        email = parse_eml(path.read_bytes())
        pred = extract(email, backend, settings)
        label = Label(
            reviewed=False,
            items=[
                LabelItem(
                    type=i.type,
                    title=i.title,
                    start_at=(i.start_at.date().isoformat() if i.all_day else i.start_at.strftime("%Y-%m-%dT%H:%M"))
                    if i.start_at else None,
                    amount=i.amount,
                    currency=i.currency,
                )
                for i in pred.items
            ],
            transactions=[
                LabelTransaction(source=t.source, merchant=t.merchant, amount=t.amount, currency=t.currency,
                                 category=t.category, is_refund=t.is_refund,
                                 purchased_at=t.purchased_at.date())
                for t in pred.transactions
            ],
            notes=f"DRAFT from {backend.name} | {email.subject}",
        )
        label_path.write_text(label.model_dump_json(indent=2), encoding="utf-8")
        print(f"drafted {label_path}")


if __name__ == "__main__":
    main()
