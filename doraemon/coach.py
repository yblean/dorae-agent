"""Dorae-3's answers: your day, your week and your targets as cards, and reading what you typed.

- food_request(): "had chicken rice and kopi", "lunch: laksa", or in Dorae-3's own chat just "chicken rice and
  kopi": the foods to log. Questions ("how much protein today?") are never taken for food.
- parse_target(): "set my protein target to 140" -> {"protein": 140}, set straight away (it's your own number).
- day_card / week_card / targets_card: the cards, with every number worked out from your log and steps.
"""
import re
from datetime import date, timedelta

from doraemon.fitness import TARGET_NAMES, Fitness

FOOD_LOG = re.compile(r"^\W*(?:(?:i |just |i just )?(?:ate|had|eaten|drank)|log(?:ged)?(?: food)?|"
                      r"(?:for )?(?:breakfast|brunch|lunch|dinner|supper|snack|tea)(?: was| is)?)\b[:,-]?\s*(?P<what>.+)$", re.I)
FOR_MEAL = re.compile(r"^(?P<what>.+?)\s+for\s+(?:breakfast|brunch|lunch|dinner|supper|snack|tea)\W*$", re.I)
QUESTION = re.compile(r"\?|^\W*(?:what|how|why|when|which|who|where|can|could|should|is|am|are|do|does|did|will|would|"
                      r"tell|show|give|any|list|help|set|suggest|recommend|change|remove|delete|undo|hi|hello|hey|thanks?)\b",
                      re.I)
TARGET = re.compile(r"\bset (?:my )?(?:daily )?(?P<what>calories?|calorie|kcal|protein|steps?)(?: (?:target|goal|intake))?"
                    r"\s*(?:to|at|=)?\s*(?P<n>\d[\d,]*)", re.I)
SUGGEST = re.compile(r"\b(?:suggest|recommend|work out|calculate|what should)\b.*\b(?:targets?|goals?|calories|protein)\b",
                     re.I)


def food_request(text: str, own_chat: bool) -> str | None:
    """The foods to log in a message, or None if it isn't one. In Dorae-3's chat a plain statement counts too.
    Elsewhere (Telegram) "had ..." alone isn't enough ("had a meeting"): it takes ate, drank, log or a meal word."""
    text = text.strip()
    if (m := FOOD_LOG.match(text)) and (own_chat or not re.match(r"^\W*(?:i |just )*had\b", text, re.I)):
        return m["what"].strip(" .")
    if m := FOR_MEAL.match(text):
        return m["what"].strip(" .")
    if own_chat and text and not QUESTION.search(text) and len(text) <= 200:
        return text.strip(" .")
    return None


def parse_target(text: str) -> dict | None:
    m = TARGET.search(text)
    if not m:
        return None
    what = m["what"].lower()
    key = "protein" if what.startswith("protein") else "steps" if what.startswith("step") else "kcal"
    return {key: int(m["n"].replace(",", ""))}


def short_day(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def progress(value: float | None, target: int | None, unit: str) -> str:
    v = f"{value:,.0f}" if value is not None else "–"
    return f"{v} of {target:,}{unit}" if target else f"{v}{unit}"


def day_card(fit: Fitness, day: date, label: str) -> dict:
    s = fit.day_status(day)
    t, goals = s["totals"], s["targets"]
    parts = [f"{progress(t.kcal, goals['kcal'], ' kcal')}", f"{progress(t.protein, goals['protein'], ' g')} protein"]
    if s["steps"] is not None or goals["steps"]:
        parts.append(f"{progress(s['steps'], goals['steps'], '')} steps" if s["steps"] is not None else "no steps yet")
    if not t.foods:
        text = (f"{label}: nothing logged yet" + (f", {parts[-1]}" if s["steps"] is not None else "") + ". Tell me what "
                "you ate, like “chicken rice and kopi siew dai”.")
    else:
        text = f"{label}: " + ", ".join(parts) + "."
        left = s["left"]
        if goals["protein"] and left["protein"] and left["protein"] > 0:
            text += f" {left['protein']:.0f} g protein to go."
        if goals["kcal"] and left["kcal"] is not None:
            text += f" {left['kcal']:.0f} kcal left." if left["kcal"] >= 0 else f" {-left['kcal']:.0f} kcal over."
    if not any(goals.values()):
        text += " Set your targets on the Fitness page (or ask me to suggest them) to see how you're doing."
    return {"text": text, "kind": "fitness_day", "payload": {
        "day": day.isoformat(), "label": label, "targets": goals, "steps": s["steps"],
        "eaten": {m: round(getattr(t, m)) for m in ("kcal", "protein", "carbs", "fat", "sugar")},
        "foods": [[r["id"], r["name"], r["portion"] or "", round(r["kcal"]), round(r["protein"])] for r in s["foods"]]}}


def week_card(fit: Fitness, which: str = "this") -> dict:
    today = fit.today()
    monday = today - timedelta(days=today.weekday()) - timedelta(days=7 if which == "last" else 0)
    last = min(monday + timedelta(days=6), today)
    rows, goals = fit.days(monday, last), fit.targets()
    logged = [r for r in rows if r["foods"]]
    stepped = [r for r in rows if r["steps"] is not None]
    label = f"{'Last' if which == 'last' else 'This'} week, {short_day(monday)} – {short_day(last)}"
    if not logged and not stepped:
        text = f"{label}: nothing logged yet."
    else:
        bits = []
        if logged:
            avg_k = sum(r["kcal"] for r in logged) / len(logged)
            avg_p = sum(r["protein"] for r in logged) / len(logged)
            bits.append(f"you logged {len(logged)} of {len(rows)} days, averaging {avg_k:,.0f} kcal"
                        + (f" (target {goals['kcal']:,})" if goals["kcal"] else "")
                        + f" and {avg_p:.0f} g protein" + (f" (target {goals['protein']})" if goals["protein"] else ""))
            if goals["protein"]:
                hit = sum(1 for r in logged if r["protein"] >= goals["protein"] * 0.95)
                bits.append(f"protein hit on {hit} day{'s' if hit != 1 else ''}")
        if stepped:
            avg_s = sum(r["steps"] for r in stepped) / len(stepped)
            bits.append(f"steps averaged {avg_s:,.0f}" + (
                f", {sum(1 for r in stepped if r['steps'] >= goals['steps'])} day(s) at {goals['steps']:,}" if goals["steps"] else ""))
        text = f"{label}: " + "; ".join(bits) + "."
    return {"text": text, "kind": "fitness_week", "payload": {"label": label, "targets": goals, "rows": [
        {"day": r["day"].isoformat(), "name": f"{r['day']:%a}", "kcal": round(r["kcal"]), "protein": round(r["protein"]),
         "foods": r["foods"], "steps": r["steps"]} for r in rows]}}


def targets_card(fit: Fitness, suggested: dict | None = None) -> dict:
    goals = fit.targets()
    if suggested:
        text = (f"From your profile I'd suggest {suggested['kcal']:,} kcal, {suggested['protein']} g protein and "
                f"{suggested['steps']:,} steps a day. Change anything, then press Save.")
    elif any(goals.values()):
        text = "Your daily targets: " + ", ".join(
            f"{goals[k]:,}{' kcal' if k == 'kcal' else ' g protein' if k == 'protein' else ' steps'}"
            for k in TARGET_NAMES if goals[k]) + "."
    else:
        text = ("You haven't set targets yet. Enter them below, or fill in your profile on the Fitness page and I'll "
                "suggest some.")
    return {"text": text, "kind": "targets", "payload": {"targets": goals, "suggested": suggested}}


def answer(fit: Fitness, route) -> dict:
    """The card for one of Dorae-3's lookups (doraemon.router: fit_day, fit_week, fit_targets)."""
    if route.intent == "fit_week":
        return week_card(fit, route.slots.get("which", "this"))
    if route.intent == "fit_targets":
        return targets_card(fit)
    day = fit.today() - timedelta(days=1 if route.slots.get("day") == "yesterday" else 0)
    return day_card(fit, day, f"{'Yesterday' if day != fit.today() else 'Today'}, {short_day(day)}")


def food_lines(rows) -> list[str]:
    """A draft or logged meal as plain lines (Telegram)."""
    lines = [f"• {r['name']}" + (f" ({r['portion']})" if r["portion"] else "")
             + f": {r['kcal']:.0f} kcal, {r['protein']:.0f} g protein, {r['carbs']:.0f} g carbs, {r['fat']:.0f} g fat"
             + (" · your usual" if r["source"] == "yours" else " · from the label database" if r["source"] == "database" else "")
             for r in rows]
    total_k, total_p = sum(r["kcal"] for r in rows), sum(r["protein"] for r in rows)
    if len(rows) > 1:
        lines.append(f"Total: {total_k:.0f} kcal, {total_p:.0f} g protein")
    return lines
