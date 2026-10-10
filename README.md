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
- **Overseas spending is converted** to `DORAEMON_HOME_CURRENCY` at the European Central Bank rate on
  the purchase date (Frankfurter API, no key; only currency codes and dates are sent). Rates are fetched
  during ingest and cached, so the review page works offline. `DORAEMON_CONVERT_CURRENCIES=off` keeps
  foreign payments out of the totals instead.
- **Pay-later bookings count as spending** on the date the card will be charged.
- **PDF attachments are read** (`pypdf`) and passed to the model with the email body.
- **Hotel stays are all-day items** on the check-in date; check-in windows and hotel timezones are ignored.
- **Flight and train times are local to the departure place** (Narita departures in Japan time,
  Changi departures in Singapore time). The model guesses the timezone and sometimes picks the
  wrong airport, so review cards show the departure place, local time and your time
  (`doraemon/display.py`) to make a wrong guess easy to spot and correct.
- **Numeric dates are day-first** (`02/10/26` is 2 October). Set `DORAEMON_DATE_ORDER=MDY` to change.
- **Fixed spending categories** (see `Category` in `doraemon/schema.py`).

## Connect Gmail

One-time setup in Google Cloud (about 15 minutes). Doraemon asks only for **read-only** mail access.

1. Go to <https://console.cloud.google.com>, create a project (e.g. `doraemon`).
2. **APIs & Services → Library**: enable the **Gmail API** (and the **Google Calendar API**, for later).
3. **Google Auth Platform → Branding**: app name `Doraemon`, your email as support contact.
4. **Audience**: user type **External**, then add your own Gmail address under **Test users**.
5. **Clients → Create client**: type **Desktop app**. Download the JSON and save it as
   `data/google/credentials.json` (gitignored with the rest of `data/`).
6. Run:
   ```
   python -m doraemon.ingest --days 30
   ```
   Your browser opens: sign in, and on "Google hasn't verified this app" choose **Continue**
   (it's your own app). Approve read-only Gmail access. The token is saved to `data/google/token.json`.

After that, each run asks Gmail only for mail since the last complete check (with an hour's overlap
for late arrivals) and skips emails it has already read. While the web app is open, Dorae-2 does
this every 15 minutes and posts in its chat when something new turns up. Set
`DORAEMON_POLL_MINUTES` to change the interval, or `0` to turn it off ("Run now" still works).

While the app is in **Testing**, Google expires the sign-in after 7 days, so ingest will ask you to
sign in again weekly. Publishing it (Audience → **Publish app**) removes that for personal use;
you'll keep seeing the "unverified app" screen, which is expected for an app only you use.

### Connect Google Calendar

Confirmed items go into a separate **Doraemon** calendar that the app creates. It uses the
`calendar.app.created` permission, so it can only change that one calendar. To show your week, it also
reads your other calendars' events (`calendar.events.readonly` and `calendar.calendarlist.readonly`),
which can never change them.

1. In Google Cloud, **APIs & Services → Library**: enable the **Google Calendar API**.
2. Run `python -m doraemon.calendar_sync connect` and approve in your browser (one approval covers Gmail and Calendar).
   If you connected before the week view existed, run it again to approve reading your calendars.

After that, **Confirm** adds the event (with reminders and a link to the email) and **Undo** removes it.
Bills, deadlines, deliveries and RSVPs don't go to the calendar: they get Telegram reminders instead (below).
`python -m doraemon.calendar_sync push` adds anything you confirmed before connecting.
Ask Dorae-2 "Show my schedule this week" (or "next week") for a week view of all the calendars you
show in Google Calendar, plus things from your email that aren't in it yet.

Triage skips the Gmail Promotions, Social and Forums tabs before the model runs, unless the
subject looks like an order, booking, bill or delivery.

### Telegram reminders

Bills, deadlines (e.g. module registration), deliveries and RSVPs show **Remind me** instead of Confirm.
Pick when the first reminder comes (1 week, 3 days, 1 day, 3 hours, 1 hour before, or at the time) and how
many (1–4); later ones step down that list, so "1 day before, 3 times" is 1 day, 3 hours and 1 hour before.
All-day items count from 9am on the day and use whole days only ("the day before", "that morning").
Change them later from **Upcoming**; **Undo** on the confirm cancels them. The bot messages you right away when
you set or change reminders, listing when they'll come.

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token into `.env` as `DORAEMON_TELEGRAM_TOKEN`.
2. Run `python -m doraemon.telegram connect`, then send your new bot any message (e.g. `/start`).
   It remembers that chat, so reminders only go to you. `python -m doraemon.telegram test` sends a test message.

Reminders are sent by the web app, so it has to be running. If it was closed when one was due, the latest
missed reminder goes out when it starts again, unless the item has already passed. Messages are plain text
with a link to the email.

You can also ask Dorae-2 for one: "create me a reminder to get groceries tmr" or "remind me to call mom
at 5pm tomorrow". It drafts a card with the title, date, time and reminder settings filled in (once, at the
time, or 9am for a day without a time); change anything, then press **Create reminder**. The app reads the
date from your own words (it understands tmr, in 2 hours, next monday at 9am, a bare 8pm); clear requests
like these don't wait for the model, and the model handles odder wording.

**Item types.** The Type dropdown (under Edit · more options, and on reminder cards) groups types by
where they go: Telegram reminders (Reminder, Bill, Deadline, Delivery, RSVP) or Google Calendar events
(Appointment, Flight, Hotel, Train, bus or ferry). Pick **+ New type…** to make your own, like "Study" or
"Gym", and choose which of the two it works like. Delete your types under Learned rules; their items keep
working as before.

Send the bot **/reminders** to get a list of your upcoming reminders (each item with the times it will
remind you). It only answers your own chat, and only while the web app is running.

### Morning briefing

Every day at `DORAEMON_BRIEFING_TIME` (default `08:00`, `off` to turn it off) Dorae-2 posts a briefing in
its chat: today and tomorrow from all your Google calendars plus email and chat items not in Google yet,
bills due in the next 7 days, trips in the next 14, Telegram reminders going out today, and what still
needs your OK. Fixed rules pick everything and the text is a template, so no model is involved. It only
goes out while the web app runs; if it wasn't running at that time, the briefing is posted when you
next open Dorae-2's chat. Before the briefing time, opening the chat doesn't post an overview (except
in an empty chat), so the day's briefing isn't used up early.

## Chat with the agents

Dorae-1 (spending) and Dorae-2 (calendar) answer free-form questions like "what's my latest purchase?"
or "any bills due this month?". The local model (`DORAEMON_CHAT_MODEL`, default: your `DORAEMON_MODEL`)
calls read-only tools over your database and Google calendars; the card under each answer is built
from the tool results, not from the model's words.

Ask Dorae-2 to add an event ("add date night on 26/10 19:00") and it drafts a card waiting for your OK;
**Confirm** puts it in your Doraemon calendar. The app reads the date from your own wording (day first,
per `DORAEMON_DATE_ORDER`), not the model, and keeps an end time only if you typed one.

Guardrails: a topic check runs first, so Dorae-1 points calendar questions to Dorae-2 (and the other
way round) and declines anything else; each agent only has its own tools; chat can't change
existing items, and a drafted event only reaches Google Calendar when you press Confirm. If the
model isn't running, the agents fall back to fixed answers. Set `DORAEMON_CHAT_MODEL=off` to always
use those.

```
python -m evals.chat_eval      # 21 questions: right topic, right tool, stays in its lane
```

## Budgets and charts (Dorae-1)

Set monthly budgets, for all spending or per category, on the **Budgets** page (the grey hints are your
average over recent full months), or tell Dorae-1 "set my dining budget to 300" and press **Save** on the
card. Each budget shows how much is used, with a tick for how far through the month you are. Ask
"how can I stay within my budget?" and the app works out the tips (what you can spend a day, where the
money went, categories heading above usual), which the model only puts into words. After each inbox
check, Dorae-1 posts in its chat when a budget reaches 80% and 100% (once per level per month).

Breakdowns are donut charts with every category listed below. "Is my spending going up or down?" shows
spending per month as columns, with this month lighter because it isn't over yet; All payments and
Budgets show the same chart. Months before your read email starts are left out of the chart and the
averages, since they only hold a few payments from later statements.

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
- `doraemon/chat.py`: the agents' tool-calling chat and its guardrails
- `evals/`: labels, scoring, helper scripts
