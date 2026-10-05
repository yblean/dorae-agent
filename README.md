# Doraemon

Inbox to Action, v0 (single user). Currently at milestones 1–3: extraction + eval.

## Setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env      # set your timezone and currency
pytest
```

Install [Ollama](https://ollama.com) and pull a model: `ollama pull qwen3:4b`.

## Build the labeled set

1. Export mail: Google Takeout → `.mbox`, then `python -m evals.import_mbox "path\to\mail.mbox" --limit 300`,
   or save single emails as `.eml` into `data/emails/`.
2. Delete the ones you don't want; keep a mix (bills, appointments, receipts, travel, promos, tricky cases).
3. `python -m evals.draft_labels` writes a model-guessed label per email into `data/labels/`.
4. Open each draft, fix it against the email, set `"reviewed": true`. Format: see `evals/labels.py`.

`data/` is gitignored. Real mail never goes in git.

## Score a model

```
python -m evals.run_eval --model ollama:qwen3:4b
python -m evals.run_eval --model ollama:gemma3:4b
```

Prints the spec metrics plus every miss, wrong field and extra suggestion.
Predictions are saved to `evals/results/` so runs can be compared.

## Decisions

- **Single user.** Settings live in `.env`; no users table yet.
- **Bank alerts are the main source of spending.** Every card and payment-app alert
  (Trust, PayLah, MariBank) becomes a transaction, categorized from the payee name.
  A merchant receipt counts only when no bank alert covers it: `doraemon/ledger.py` drops
  a receipt whose amount matches an alert within 3 days, or that has the same merchant
  name and is within 5% of it (Trip Coins, vouchers). Money received is not spending.
- **Pay-later bookings count as spending** on the date the card will be charged.
- **PDF attachments are read** (`pypdf`) and passed to the model with the email body.
- **Hotel stays are all-day items** on the check-in date; check-in windows and hotel timezones are ignored.
- **Flight and train times are local to the departure place** (Narita departures in Japan time,
  Changi departures in Singapore time). The model guesses the timezone and sometimes picks the
  wrong airport, so review cards show the departure place, local time and your time
  (`doraemon/display.py`) to make a wrong guess easy to spot and correct.
- **Numeric dates are day-first** (`02/10/26` is 2 October). Set `DORAEMON_DATE_ORDER=MDY` to change.
- **Fixed spending categories** (see `Category` in `doraemon/schema.py`).

## Rules

Corrections are remembered as rules and applied in code, so the same mistake isn't made twice.
Built-in rules (SG merchants, airport timezones) are in `doraemon/rules/defaults.toml`; yours are
in `data/doraemon.db` and win over the defaults.

```
python -m doraemon.rules list --defaults
python -m doraemon.rules add merchant_category "Kopitiam" dining
python -m doraemon.rules add ignore_sender news@shop.example
python -m doraemon.rules delete 3
```

Rule types: `merchant_category`, `ignore_sender` (skips the email before the model runs),
`travel_timezone` (departure place -> timezone), `not_spending` (e.g. transfers to family).

To measure rules without re-running the model, replay a previous run:

```
python -m evals.run_eval --reuse evals/results/<run>.jsonl --rules none
python -m evals.run_eval --reuse evals/results/<run>.jsonl --rules defaults --learn
```

`--learn` turns each email's label corrections into rules, oldest email first.

## Layout

- `doraemon/schema.py`: Pydantic models (raw model output vs. final items)
- `doraemon/extract.py`: prompt, model call, validation, safety checks
- `doraemon/dates.py`: deterministic date parsing (the model never computes dates)
- `doraemon/llm.py`: swappable model backends
- `evals/`: labels, scoring, helper scripts
