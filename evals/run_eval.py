"""Score the pipeline against the labeled emails.

    python -m evals.run_eval                          # model from .env, built-in rules
    python -m evals.run_eval --model ollama:qwen3:4b
    python -m evals.run_eval --rules none             # model alone
    python -m evals.run_eval --learn                  # corrections become rules as it goes
    python -m evals.run_eval --reuse evals/results/<run>.jsonl --learn
                                                      # replay a run's model output: seconds, not minutes

Emails are processed oldest first, so with --learn a rule only helps emails after
the correction that created it, like it would in real use.
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

import httpx

from doraemon.config import Settings
from doraemon.email_parse import parse_eml
from doraemon.extract import extract
from doraemon.llm import get_backend
from doraemon.pipeline import process, should_skip
from doraemon.rules import RuleStore
from doraemon.schema import Extraction
from evals.corrections import learn
from evals.labels import load_label
from evals.scoring import Score


def load_raw(path: Path) -> dict[str, Extraction]:
    """Model output (before rules) from an earlier run, by email name."""
    raw = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        raw[row["email"]] = Extraction.model_validate(row.get("raw") or row["pred"])
    return raw


def main() -> None:
    settings = Settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=settings.model)
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--rules", choices=["none", "defaults"], default="defaults",
                    help="start with no rules, or with the built-in defaults")
    ap.add_argument("--learn", action="store_true", help="turn each email's corrections into rules")
    ap.add_argument("--reuse", type=Path, help="replay model output from this results file")
    args = ap.parse_args()

    backend = get_backend(args.model, settings)
    cached = load_raw(args.reuse) if args.reuse else {}
    rules = RuleStore(":memory:", use_defaults=args.rules == "defaults")  # never touches your real rules
    score = Score()

    config = f"rules={args.rules}" + (" +learn" if args.learn else "")
    out_dir = Path("evals/results")
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{args.model.replace(':', '_')}-{args.rules}{'-learn' if args.learn else ''}"
    out_path = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{tag}.jsonl"

    # Oldest first, so learned rules only reach later emails
    labeled, skipped = [], 0
    for path in sorted((args.data / "emails").glob("*.eml")):
        label_path = args.data / "labels" / f"{path.stem}.json"
        label = load_label(label_path) if label_path.exists() else None
        if label is None or not label.reviewed:
            skipped += 1
            continue
        labeled.append((path.stem, parse_eml(path.read_bytes()), label))
    labeled.sort(key=lambda row: row[1].sent_at)

    learned: list[str] = []
    applied = 0
    with out_path.open("w", encoding="utf-8") as out:
        for name, email, label in labeled:
            raw = cached.get(name)
            if raw is None and not should_skip(email, rules):
                try:
                    raw = extract(email, backend, settings)
                except httpx.HTTPError as e:
                    raw = Extraction(message_id=email.message_id, problems=[f"model call failed: {e!r}"])
            pred = process(email, backend, settings, rules, raw=raw or Extraction(message_id=email.message_id))
            score.add(name, pred, label, settings.timezone)
            applied += len(pred.applied_rules)
            for note in pred.applied_rules:
                score.failures.append(f"{name}: RULE {note}")
            if args.learn:
                learned += [f"{name}: {r}" for r in learn(rules, name, email, pred, label)]

            out.write(json.dumps({
                "email": name,
                "raw": raw.model_dump(mode="json") if raw else None,
                "pred": pred.model_dump(mode="json"),
            }) + "\n")
            out.flush()  # keep finished emails if the run is interrupted
            print(f"{name}: {len(pred.items)} items, {len(pred.transactions)} txns, "
                  f"{len(pred.applied_rules)} rules, {pred.latency_s:.1f}s", flush=True)
            if args.limit and score.emails >= args.limit:
                break

    print(f"\nModel: {backend.name}   Config: {config}   Emails scored: {score.emails}   "
          f"Skipped (no reviewed label): {skipped}\n")
    for metric, value in (score.summary() | score.ledger()).items():
        print(f"{metric:42} {value}")
    print(f"{'   Rule applications':42} {applied}")
    if learned:
        print(f"\nRules learned from corrections ({len(learned)}):")
        for line in learned:
            print("  " + line)
    if score.failures:
        print("\nFailures and rule applications:")
        for f in score.failures:
            print("  " + f)
    print(f"\nPredictions saved to {out_path}")


if __name__ == "__main__":
    main()
