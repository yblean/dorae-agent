"""Dorae-3's answers: your day, your week and your targets as cards, and reading what you typed.

- food_request(): "had chicken rice and kopi", "lunch: laksa", or in Dorae-3's own chat just "chicken rice and
  kopi": the foods to log. Questions ("how much protein today?") are never taken for food.
- parse_target(): "set my protein target to 140" -> {"protein": 140}, set straight away (it's your own number).
- day_card / week_card / targets_card: the cards, with every number worked out from your log and steps.
- review_card: several weeks (or a week or month) against your targets, with up to 3 things to improve.
"""
import re
from datetime import date, timedelta

from doraemon.fitness import KCAL_PER_GRAM, STEPS_PER_MINUTE, TARGET_NAMES, Fitness
from doraemon.foodlog import given_numbers

# "had chicken rice", "can you log 2 eggs", "this morning i had oats", "im eating laksa", "my lunch was laksa",
# "lunch: laksa" (not "tea and biscuits", which is food itself, or "lunch with Sam")
_POLITE = r"(?:(?:can|could|would|will|pls|please|plz|kindly)\s+(?:you\s+|u\s+)?)*"
_WHEN = r"(?:(?:this\s+(?:morning|afternoon|evening)|earlier(?:\s+today)?|just\s+now|today|tonight|yesterday)\W*\s+)?"
_ME = r"(?:(?:i|i'?ve|i\s+have|i'?m|i\s+am)\s+)?(?:just\s+|also\s+|already\s+)?"
_ATE = (r"(?:ate|had|have\s+had|eaten|drank|drunk|eating|having|drinking|finished|snacked\s+on|"
        r"log(?:ged)?|add|track|record)(?:\s+(?:food|this|these))?")
_MEAL = (r"(?:for\s+|my\s+)?(?:breakfast|brunch|lunch|dinner|supper|snack|tea)(?!\s+(?:and|with|or|&)\b)"
         r"(?:\s+(?:was|is|today|just\s+now))?")
FOOD_LOG = re.compile(r"^\W*" + _POLITE + _WHEN + r"(?:" + _ME + r"(?P<verb>" + _ATE + r")|" + _MEAL + r")\b"
                      r"[:,-]?\s*[:,-]?\s*(?P<what>.+)$", re.I | re.S)
FOR_MEAL = re.compile(r"^(?P<what>.+?)\s+for\s+(?:breakfast|brunch|lunch|dinner|supper|snack|tea)(?:\s+(?:today|yesterday|"
                      r"tonight|just now|earlier(?: today)?|this (?:morning|afternoon|evening)))?\W*$", re.I)
# "a protein shake", "whey and a banana": foods, though "protein" makes them look like a question about your protein
PROTEIN_FOOD = re.compile(r"\b(?:protein|whey|casein)\s+(?:shakes?|bars?|powder|balls?|drinks?|milk|yog(?:h)?urts?|"
                          r"puddings?|cookies?|chips|bites|waffles?|pancakes?|bread|noodles|pasta|ice cream|smoothies?|"
                          r"coffee|latte|water|wraps?|scoops?)\b|\b(?:whey|casein)\b", re.I)
_TAIL = re.compile(r"\s+(?:for me|to (?:my|the) (?:food )?(?:log|diary|tracker|day|list)|please|pls|plz|thanks?|thx|"
                   r"ty|now|just now|today|earlier(?: today)?|this (?:morning|afternoon|evening)|yesterday|tonight)\W*$",
                   re.I)
# On Telegram "had" or "add" may not be food: "had a long meeting", "had to go", "track my protein"
_NOT_FOOD = re.compile(
    r"^\W*(?:to\b|(?:(?:a|an|the|my|our|some|such|so|been|really|very|pretty)\s+)*(?:[\w']+\s+){0,2}?(?:meetings?|"
    r"calls?|chats?|talks?|class(?:es)?|lessons?|lectures?|appointments?|sessions?|interviews?|days?|time|nights?|"
    r"mornings?|afternoons?|evenings?|weeks?|weekends?|fun|dreams?|naps?|sleep|showers?|baths?|workouts?|runs?|walks?|"
    r"swims?|gym|training|games?|match(?:es)?|look|try|fights?|arguments?|party|parties|trips?|exams?|tests?|fever|"
    r"headaches?|flu|cough|issues?|problems?|questions?|ideas?|feelings?|blast|rest|haircut|massages?|visits?|"
    r"reminders?|events?|tasks?|reports?|work|homework|assignments?)\b|"
    r"my\s+(?:calories|kcal|protein|carbs|macros|steps|weight|progress|intake|targets?|goals?)\b)", re.I)
# Telegram's chatter, never worth asking the model about
_CHATTER = re.compile(r"^\W*(?:ok(?:ay)?|k|kk|lol|lmao|haha\w*|hehe\w*|test(?:ing)?|cool|nice|yes|yeah|yep|yup|ya|no|"
                      r"nope|nah|sure|good|great|done|bye|gn|gm|good (?:morning|night)|sorry|oops|hm+|ah+|oh+)\W*$", re.I)
QUESTION = re.compile(r"\?|^\W*(?:what|how|why|when|which|who|where|can|could|should|is|am|are|do|does|did|will|would|"
                      r"tell|show|give|any|list|help|set|suggest|recommend|change|remove|delete|undo|hi|hello|hey|thanks?)\b",
                      re.I)
# "set my protein target to 140", "change my daily calories to 1950", "lower my calories by 100", "my step goal should
# be 10k", "i want to eat 1950 calories a day": a verb (or one of those shapes) and one or more "what ... number"
_WHAT = r"(?P<what>cal(?:orie)?s?|kcal|protein|steps?)"
_NUMBER = r"(?P<n>\d[\d,]*(?:\.\d+)?(?:\s*k\b)?)(?:\s*(?:g|grams?)\b)?"
_CHANGE = re.compile(r"\b(?:set|change|make|update|put|move|raise|increase|lower|reduce|drop|cut|bump|adjust|switch|"
                     r"up\s+(?:my|the)|bring)\b|\bmy\s+(?:daily\s+)?(?:cal(?:orie)?s?|kcal|protein|steps?)\s+(?:target|goal|intake|limit)\s+"
                     r"(?:should be|is now|to be)\b|\bi\s+(?:want|wanna|need|plan)\s+to\s+(?:eat|have|hit|get|walk|do)\b", re.I)
_WONDERING = re.compile(r"\W*(?:how|what|whats|why|should|would|is|does|do|when|which)\b", re.I)  # not "can you..."
_DOWN = re.compile(r"\b(?:lower|reduce|drop|cut|decrease)\b", re.I)
TARGET = re.compile(r"\b" + _WHAT + r"(?:\s+(?:target|goal|intake|limit|budget))?(?:\s+(?:a|per|each|every)\s+day)?"
                    r"\s*(?:should be|is now|to be|to|at|=|:|of|by)?\s*" + _NUMBER
                    + r"|\b" + _NUMBER.replace("?P<n>", "?P<n2>") + r"\s*" + _WHAT.replace("?P<what>", "?P<what2>"), re.I)
SENSIBLE = {"kcal": (800, 6000), "protein": (20, 400), "steps": (500, 60000)}
SUGGEST = re.compile(r"\b(?:suggest|recommend|work out|calculate|what should)\b.*\b(?:targets?|goals?|calories|protein)\b",
                     re.I)
# "how much will a bowl of greek yogurt be in cals and protein", "calories in chicken rice": about a food, not you
_NUTRIENT = re.compile(r"\b(?:cal(?:orie)?s?|kcal|protein|carbs?|carbohydrates?|fats?|sugar|macros?|nutrition(?:al)?)\b", re.I)
_ABOUT_YOU = re.compile(r"\b(?:i|i'?ve|i'?m|im|ive|my|me|today|tonight|yesterday|left|remaining|so far|this week|last week|"
                        r"targets?|goals?|should)\b", re.I)
_ASKING = re.compile(r"^\W*(?:how (?:much|many)|what(?:'s| is| are|s)?|whats|is|are|does|do|can you tell me|tell me|give me|"
                     r"nutrition|calories|cals|protein|macros)\b|\?\s*$", re.I)
_QUESTION_WORDS = re.compile(
    r"\b(?:how (?:much|many)|what(?:'s| is| are)?|whats|tell me|give me|can you tell me|the|will|would|does|do|is|are|be|"
    r"has|have|contains?|there|in|of|for|and|cal(?:orie)?s?|kcal|protein|carbs?|carbohydrates?|fats?|sugar|macros?|"
    r"nutrition(?:al)?|info(?:rmation)?|amount|approx(?:imately)?|roughly|about|it|like)\b", re.I)


# A judgement about foods ("which has more protein, tofu or eggs?", "is milo high in sugar?", "is laksa healthy?") or
# about your own eating ("is 67 g of fat a day too much?", "is my diet balanced?"): for the model, which looks foods
# up with its nutrition tool and your intake with nutrient_facts
FOOD_QUESTION = re.compile(r"(?=.*\b(?:cal(?:orie)?s?|kcal|protein|carbs?|fats?|sugars?|macros?|nutrition|healthy|healthier|"
                           r"nutritious|unhealthy|diet)\b)(?=.*\b(?:which|compare[ds]?|vs|versus|better|healthier|worse|"
                           r"more|less|high|higher|low|lower|healthy|unhealthy|good|bad|too much|too little|enough|or|"
                           r"balanced|cut|cutting|reduce)\b)", re.I | re.S)


def nutrition_subject(text: str) -> str | None:
    """The food a nutrition question is about ("a bowl greek yogurt"), or None if it isn't one (or it's about you:
    "how much protein have I had today" is your day, not a food)."""
    if not (_NUTRIENT.search(text) and _ASKING.search(text)) or _ABOUT_YOU.search(text):
        return None
    if FOOD_QUESTION.search(text):
        return None  # "which has more protein, tofu or eggs?", "is milo high in sugar?": the model weighs it up
    if PROTEIN_FOOD.search(text) and not QUESTION.search(text):
        return None  # "protein shake" is what you had
    kept = PROTEIN_FOOD.sub(lambda m: m[0].replace(" ", "_"), text.lower())  # "calories in a protein bar": a bar's
    subject = " ".join(_QUESTION_WORDS.sub(" ", re.sub(r"[?!.,]", " ", kept)).split()).replace("_", " ")
    return subject if re.search(r"[a-z]{3}", subject) else None


def nutrition_text(rows) -> str:
    """'A bowl of Greek yogurt (200 g): about 190 kcal, 18 g protein, ...' for each food, and the total."""
    parts = []
    for r in rows:
        where = (" (your usual)" if r["source"] == "yours" else f" (from {r['note']})" if r["source"] == "database"
                 else " (from your label photo)" if r["source"] == "label" else " (an estimate)")
        parts.append(f"{r['name']}" + (f", {r['portion']}" if r["portion"] else "") + f": about {r['kcal']:.0f} kcal, "
                     f"{r['protein']:.0f} g protein, {r['carbs']:.0f} g carbs, {r['fat']:.0f} g fat, {r['sugar']:.0f} g "
                     f"sugar{where}.")
    if len(rows) > 1:
        parts.append(f"Together: {sum(r['kcal'] for r in rows):.0f} kcal, {sum(r['protein'] for r in rows):.0f} g protein.")
    return " ".join(parts)


LEAN_ROOM = 150  # kcal: less left than this and ideas turn to the leanest protein
# "suggest some foods to hit my targets", "what should I eat for dinner?": ideas to eat, for the model (not targets)
FOOD_IDEAS = re.compile(r"^(?=.*\b(?:eat|eating|food|foods|meal|meals|snack|snacks|dinner|lunch|breakfast|supper|order|"
                        r"menu|recipes?)\b).*\b(?:suggest|recommend|ideas?|options?|what (?:should|can|could|do) i|"
                        r"what to|help me|give me|any)\b", re.I | re.S)


def _just_food(what: str) -> str:
    """'chicken rice' from 'chicken rice for lunch', 'chicken rice to my food log', 'laksa now'."""
    what = what.strip(" .")
    if meal := FOR_MEAL.match(what):
        what = meal["what"]
    while (shorter := _TAIL.sub("", what)) != what:
        what = shorter
    return what.strip(" .,-?!")


def _eaten(text: str) -> str | None:
    """What follows "had", "can you log", "lunch:"..., and once more for "for lunch i had laksa" or "log my lunch:
    laksa" (but not "had tea and biscuits", where "tea" is the food)."""
    if not (m := FOOD_LOG.match(text)):
        return None
    what = m["what"]
    if (again := FOOD_LOG.match(what)) and (again["verb"] or re.search(r"(?:^\W*(?:my|for)\b.*|[:,-]\s*)$",
                                                                       what[:again.start("what")], re.I)):
        what = again["what"]
    return _just_food(what)


def food_request(text: str, own_chat: bool) -> str | None:
    """The foods to log in a message, or None if it isn't one. In Dorae-3's chat a plain statement counts too.
    Elsewhere (Telegram) it takes a word like had, ate, log or a meal, and "had a long meeting" isn't food."""
    text = text.strip()
    asked = text.endswith("?") and not re.search(r"\b(?:log|add|track|record)\b", text, re.I)  # "had enough protein?"
    if (what := _eaten(text)) is not None and not asked:
        return what if what and (own_chat or not _NOT_FOOD.match(what)) else None
    if (m := FOR_MEAL.match(text)) and not QUESTION.search(text):  # "what should I eat for dinner" is a question
        return _just_food(m["what"])
    if own_chat and text and not QUESTION.search(text) and len(text) <= 200:
        return text.strip(" .")
    return None


def food_with_numbers(text: str) -> bool:
    """"2 eggs, 12g protein", "chicken breast 3 pieces 15g protein each": a food and your numbers for it (not a
    question, and not "protein 140" about you, which names no food)."""
    if QUESTION.search(text) or _ABOUT_YOU.search(text) or not given_numbers(text):
        return False
    rest = re.sub(r"\b(?:\d+(?:\.\d+)?\s*(?:g|grams?|gm|kcal|cals?|calories?|ml)?|kcal|cals?|calories?|protein|prot|pro|p|"
                  r"carbs?|fats?|sugars?|each|and|of|with|per|its|it's|is)\b", " ", text, flags=re.I)
    return bool(re.search(r"[a-z]{3}", rest, re.I))


def food_said(text: str) -> str | None:
    """The foods to log in a Telegram message that's clearly one ("ate chicken rice", "2 eggs, 12g protein", "a
    protein shake"), or None."""
    if food := food_request(text, own_chat=False):
        return food
    if food_with_numbers(text) or PROTEIN_FOOD.search(text) and not QUESTION.search(text):
        return text.strip()
    return None


def might_be_food(text: str) -> bool:
    """A short statement that may be only foods ("3 eggs and 2 bread", "chicken rice"): worth asking the model on
    Telegram before saying what works there."""
    text = text.strip()
    return (len(text) <= 200 and bool(re.search(r"[a-z]{3}", text, re.I)) and not QUESTION.search(text)
            and not _CHATTER.match(text))


def food_day(text: str, today: date) -> date | None:
    """Yesterday's date for "yesterday i had laksa"; None for today."""
    return today - timedelta(days=1) if re.search(r"\byesterday\b", text, re.I) else None


def _number(raw: str) -> float:
    raw = raw.replace(",", "").strip().lower()
    return float(raw[:-1].strip()) * 1000 if raw.endswith("k") else float(raw)


def parse_target(text: str, current: dict | None = None) -> dict | None:
    """New daily targets you asked for: {"kcal": 1950}, or several at once ("calories 1950 and protein 140"). "By"
    changes your `current` target ("lower my calories by 100"). None if it isn't a change, or a number isn't sensible
    for that target (so "had 300 calories" or "change protein to 5" never move them)."""
    if not _CHANGE.search(text or "") or _WONDERING.match(text) or FOOD_LOG.match(text):  # "had a 1000 cal lunch"
        return None
    found = {}
    for m in TARGET.finditer(text):
        what = (m["what"] or m["what2"]).lower()
        key = "protein" if what.startswith("protein") else "steps" if what.startswith("step") else "kcal"
        value = _number(m["n"] or m["n2"])
        if m["n"] and re.search(r"\bby\s*$", text[m.start():m.start("n")], re.I):  # "by 100": from your target
            if not (current or {}).get(key):
                continue
            value = current[key] + (-value if _DOWN.search(text) else value)
        low, high = SENSIBLE[key]
        if low <= value <= high:
            found.setdefault(key, int(round(value)))
    return found or None


def target_text(target: dict) -> str:
    """"1,950 kcal and 140 g protein a day"."""
    parts = [f"{v:,} {'kcal' if k == 'kcal' else 'g protein' if k == 'protein' else 'steps'}" for k, v in target.items()]
    return (" and ".join(parts) if len(parts) < 3 else ", ".join(parts[:-1]) + f" and {parts[-1]}") + " a day"


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


def review_span(span: str, today: date) -> tuple[date, date, str]:
    """(first, last, label) for a review: "days:28" (the 28 days before today), "week:this", "week:last",
    "month:this" or "month:last". Today is left out (it isn't over) unless nothing else is in the span."""
    kind, _, which = span.partition(":")
    yesterday = today - timedelta(days=1)
    if kind == "week":
        monday = today - timedelta(days=today.weekday())
        first, last, label = ((monday - timedelta(days=7), monday - timedelta(days=1), "Last week") if which == "last"
                              else (monday, yesterday, "This week"))
    elif kind == "month":
        start = today.replace(day=1)
        first, last, label = (((start - timedelta(days=1)).replace(day=1), start - timedelta(days=1), "Last month")
                              if which == "last" else (start, yesterday, "This month"))
    else:
        n = max(2, min(int(which) if which.isdigit() else 28, 120))
        first, last = yesterday - timedelta(days=n - 1), yesterday
        label = f"The last {n // 7} weeks" if n % 7 == 0 and n > 7 else f"The last {n} days"
    return (first, last, label) if first <= last else (today, today, label)


def review_tips(fit: Fitness, r: dict) -> list[str]:
    """Up to 3 things to work on from a review (Fitness.review), most useful first; every number from your log."""
    goals, full = r["targets"], r["full"]
    if full < 3:
        return [f"Only {full} fully logged day{'s' if full != 1 else ''} so far: log every meal for a week and I can say "
                "much more."]
    tips = []
    if goals["protein"] and r["protein"] < goals["protein"] * 0.95:
        gap = goals["protein"] - r["protein"]
        room = goals["kcal"] - r["kcal"] if goals["kcal"] else None
        ideas = fit.protein_ideas(gap, max(room, 150) if room is not None else None)
        tips.append(f"Protein: about {gap:.0f} g a day short, hit on {r['protein_hit']} of {full} days."
                    + (" Adding " + " or ".join(ideas) + " on most days closes it." if ideas else ""))
    if goals["kcal"] and (r["kcal"] > goals["kcal"] * 1.05 or r["kcal_days"]["over"] * 3 >= full):
        tip = f"Calories: over {goals['kcal']:,} on {r['kcal_days']['over']} of {full} days"
        if r["over_foods"]:
            tip += ", mostly from " + " and ".join(f"{f['name'].lower()} ({f['times']}×)" for f in r["over_foods"][:2])
        if r.get("weekend_extra", 0) > 250:
            tip += f". Weekends run about {r['weekend_extra']:,.0f} kcal more than weekdays"
        tips.append(tip + ".")
    elif goals["kcal"] and r["kcal"] < goals["kcal"] * 0.85 and fit.profile().get("goal") != "lose":
        tips.append(f"Calories: {r['kcal']:,.0f} a day is well under your {goals['kcal']:,}. If that isn't on purpose, a "
                    "bigger breakfast or a protein snack closes it.")
    if goals["steps"] and r["steps"] is not None and r["steps"] < goals["steps"]:
        minutes = max(5, round((goals["steps"] - r["steps"]) / STEPS_PER_MINUTE / 5) * 5)
        tips.append(f"Steps: {r['steps']:,.0f} a day against {goals['steps']:,}. A {minutes}-minute walk a day closes it.")
    if r["logged"] < r["days"] * 0.7:
        tips.append(f"Logging: {r['logged']} of {r['days']} days logged"
                    + (f", {len(r['light'])} of them partly" if r["light"] else "")
                    + ". The more days you log, the surer these numbers are.")
    return tips[:3] or ["You're on target across the board. Keep doing what you're doing."]


def review_card(fit: Fitness, span: str = "days:28") -> dict:
    """How you did against your targets over several weeks (or a week or month), and what to improve."""
    first, last, label = review_span(span, fit.today())
    r = fit.review(first, last)
    goals, full = r["targets"], r["full"]
    label = "Since you started logging" if r["since_start"] else label
    head = f"{label}, {short_day(r['first'])} – {short_day(r['last'])}" if r["first"] != r["last"] else f"{label}, {short_day(r['first'])}"
    if not any(goals.values()):
        return {"text": f"{head}: set your targets on the Fitness page (or ask me to suggest them) and I'll tell you how "
                        "you're doing against them.", "kind": "text", "payload": None}
    if not full and r["steps"] is None:
        return {"text": f"{head}: nothing fully logged yet. Tell me what you eat for a few days, then ask again.",
                "kind": "text", "payload": None}
    rows, verdict = [], []
    if goals["kcal"] and full:
        k, d = r["kcal"], r["kcal_days"]
        state = "on" if 0.9 <= k / goals["kcal"] <= 1.1 else "over" if k > goals["kcal"] else "under"
        rows.append(["Calories", f"{k:,.0f}", f"{goals['kcal']:,} kcal", state,
                     f"on target {d['on']} of {full} days" + (f" · over {d['over']}" if d["over"] else "")
                     + (f" · under {d['under']}" if d["under"] else "")])
        verdict.append("calories on target" if state == "on" else f"calories {abs(k - goals['kcal']):,.0f} a day {state}")
    if goals["protein"] and full:
        p = r["protein"]
        state = "on" if p >= goals["protein"] * 0.95 else "under"
        rows.append(["Protein", f"{p:,.0f}", f"{goals['protein']} g", state, f"hit on {r['protein_hit']} of {full} days"])
        verdict.append("protein on target" if state == "on" else f"protein {goals['protein'] - p:,.0f} g a day short")
    if goals["steps"]:
        if r["steps"] is None:
            rows.append(["Steps", "–", f"{goals['steps']:,}", "none", "no steps recorded"])
        else:
            state = "on" if r["steps"] >= goals["steps"] else "under"
            rows.append(["Steps", f"{r['steps']:,.0f}", f"{goals['steps']:,}", state,
                         f"{r['steps_hit']} of {r['stepped']} days at target"])
            verdict.append("steps on target" if state == "on" else f"steps {goals['steps'] - r['steps']:,.0f} a day short")
    tips = review_tips(fit, r)
    text = (f"{head}: you logged {r['logged']} of {r['days']} days" + (f" ({full} fully)" if r["light"] else "")
            + (". On average, " + ", ".join(verdict) + "." if verdict else ".")
            + "\n\nTo improve:\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(tips, 1)))
    return {"text": text, "kind": "fitness_review", "payload": {
        "label": head, "rows": rows, "full": full, "tips": tips,
        "macros": {m: round(r[m]) for m in ("carbs", "fat", "sugar")} if full else None,
        "foods": [[f["name"], f["times"], round(f["kcal"])] for f in r["foods"][:3]],
        "protein_foods": [[f["name"], f["times"], round(f["protein"])]
                          for f in sorted(r["foods"], key=lambda f: -f["protein"])[:3]],
        "light": [[short_day(d), k] for d, k in r["light"]]}}


# Widely used ranges for adults, as a share of calories: the US Dietary Reference Intakes for protein, carbs and fat,
# the WHO for sugars. General guidance for the model to explain, not medical advice.
GUIDES = {
    "protein": (10, 35, "10-35% of calories (US Dietary Reference Intakes)"),
    "carbs": (45, 65, "45-65% of calories (US Dietary Reference Intakes)"),
    "fat": (20, 35, "20-35% of calories (US Dietary Reference Intakes), with saturated fat under 10%"),
    "sugar": (0, 10, "free sugars under 10% of calories, ideally under 5% (WHO)"),
}
SERVING = {"protein": 10, "carbs": 20, "fat": 8, "sugar": 5}  # grams a serving for a food to be worth suggesting
NOT_TRACKED = {"fat": "Saturated and unsaturated fat aren't logged separately, so the kind of fat can't be judged.",
               "sugar": "Logged sugar includes natural sugar (milk, fruit), so it overstates the free sugar the WHO "
                        "limit is about."}
_GRAMS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:g|grams?|gm)\b", re.I)
_NAMED = {"protein": r"protein", "carbs": r"carbs?|carbohydrates?", "fat": r"fats?", "sugar": r"sugars?"}


def asked_nutrient(question: str) -> str | None:
    """The nutrient a question is about ("is my fat too high" -> "fat"), or None for several or none."""
    named = [n for n, words in _NAMED.items() if re.search(rf"\b(?:{words})\b", question, re.I)]
    return named[0] if len(named) == 1 else None


def nutrient_facts(fit: Fitness, nutrient: str | None = None, span: str = "days:28", question: str = "") -> dict:
    """For the model: your protein, carbs, fat and sugar (or just `nutrient`) a day against the usual healthy ranges,
    with the verdict and the foods most of it comes from, all worked out here. An amount named in the question
    ("is 67 g of fat a day too much") is judged too. Shares and ranges are worked out at the calories you actually
    eat (a day on average), so the verdict and the range in grams always agree."""
    first, last, label = review_span(span, fit.today())
    r = fit.review(first, last)
    if r["since_start"]:
        label = "Since you started logging"
    full, goals = r["full"], r["targets"]
    facts = {"period": f"{label}, {short_day(r['first'])} - {short_day(r['last'])}: {full} fully logged "
                       f"day{'s' if full != 1 else ''}"}
    if not full:
        return {**facts, "nothing_logged": "No fully logged days yet, so there's nothing to judge. Say so and suggest "
                                           "logging every meal for a few days."}
    kcal = r["kcal"]
    facts["calories_a_day"] = f"{kcal:,.0f}" + (f" (target {goals['kcal']:,})" if goals["kcal"] else "")
    out = {}
    for n in ([nutrient] if nutrient in GUIDES else list(GUIDES)):
        low, high, guide = GUIDES[n]
        share = r[n] * KCAL_PER_GRAM[n] / kcal * 100
        verdict = "above the range" if share > high else "below the range" if share < low else "within the range"
        foods = sorted(r["foods"], key=lambda f: -f[n])[:3]
        out[n] = {"grams_a_day": round(r[n]), "share_of_calories": f"{share:.0f}%", "healthy_range": guide,
                  "range_in_grams": f"{low * kcal / 100 / KCAL_PER_GRAM[n]:.0f}-{high * kcal / 100 / KCAL_PER_GRAM[n]:.0f} g "
                                    f"a day at your {kcal:,.0f} kcal", "verdict": verdict,
                  "most_from": [f"{f['name']} ({f['times']}x, {f[n] / f['times']:.0f} g each)" for f in foods if f[n] > 0]}
        if n == "protein" and goals["protein"]:
            out[n]["your_target"] = f"{goals['protein']} g ({'met' if r[n] >= goals['protein'] * 0.95 else 'not met'})"
        if n == "protein" and fit.profile().get("weight_kg"):
            out[n]["per_kg_body_weight"] = f"{r[n] / float(fit.profile()['weight_kg']):.1f} g (0.8 g is the minimum; " \
                                           "1.6-2.2 g suits building or keeping muscle)"
    facts["nutrients"] = out
    conclusion = []  # what the answer says, in order: the model only puts it into words
    asked = _GRAMS.search(question)
    if asked and (named := nutrient or asked_nutrient(question)) in GUIDES:
        grams = float(asked[1])
        share = grams * KCAL_PER_GRAM[named] / kcal * 100
        low, high, _ = GUIDES[named]
        verdict = "above the range" if share > high else "below the range" if share < low else "within the range"
        facts["amount_asked_about"] = {"amount": f"{grams:g} g of {named} a day",
                                       "share_of_calories": f"{share:.0f}% of your {kcal:,.0f} kcal", "verdict": verdict}
        conclusion.append(f"{grams:g} g of {named} a day would be {share:.0f}% of the {kcal:,.0f} kcal you eat, {verdict} "
                          f"({GUIDES[named][2].split(' (')[0]}).")
    for n, o in out.items():
        top = " and ".join(f["name"].lower() for f in sorted(r["foods"], key=lambda f: -f[n])[:2] if f[n] > 0)
        said = f"Your log averages {o['grams_a_day']} g of {n} a day ({o['share_of_calories']} of calories)"
        if o["verdict"] == "above the range":
            said += f", above the healthy {o['range_in_grams'].split(' a day')[0]}, so it's worth cutting back" + (
                f": most of it comes from {top}, so have those less often or in smaller portions" if top else "")
        elif o["verdict"] == "below the range":  # more of your foods richest in it (not fatty unless it's fat, and
            # with a real amount a serving: an onion is mostly carbs, but a few grams)
            rich = sorted((f for f in r["foods"] if f["kcal"] > 0 and f[n] / f["times"] >= SERVING[n]
                           and (n == "fat" or f["fat"] * KCAL_PER_GRAM["fat"] / f["kcal"] < 0.35)),
                          key=lambda f: -f[n] * KCAL_PER_GRAM[n] / f["kcal"])[:2]
            said += f", below the healthy {o['range_in_grams'].split(' a day')[0]}, so there's room for more" + (
                f", e.g. a bit more {' or '.join(f['name'].lower() for f in rich)}" if rich else "")
        else:
            said += ", within the healthy range, so there's no need to change it"
        if n == "protein" and goals["protein"] and r[n] < goals["protein"] * 0.95:
            said += f", though it's short of your {goals['protein']} g target"
        conclusion.append(said + ".")
    facts["conclusion"] = conclusion
    missing = ([NOT_TRACKED["fat"]] if "fat" in out else []) + ([NOT_TRACKED["sugar"]] if "sugar" in out else [])
    if missing and len(out) == 1:  # one note at most, and only about what was asked
        facts["not_tracked"] = missing[0]
    facts["how"] = ("Say the period, then pass on `conclusion` in your own words and in its order, adding nothing it "
                    "doesn't say" + (", then the not_tracked note in one sentence" if "not_tracked" in facts else "")
                    + ". 2 to 5 plain sentences: no list, no 'To improve' section, and don't repeat earlier answers.")
    return facts


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


def meal_plans(options: list[dict], kcal_left: float | None, protein_left: float | None, most: int = 3,
               items: int = 3) -> list[dict]:
    """Up to `most` different plans of 1 to `items` foods from `options` that fit in kcal_left, with their totals added
    up here: the most protein towards what's still to go first, then the fewest calories, then more of your own foods.
    [{"foods": [names], "kcal", "protein", "theirs": how many are yours}]"""
    from itertools import combinations
    room = kcal_left if kcal_left is not None else 900  # no target: a normal meal's worth
    goal = protein_left if protein_left is not None and protein_left > 0 else 40
    plans = []
    for n in range(1, items + 1):
        for combo in combinations(options, n):
            kcal = sum(o["kcal"] for o in combo)
            if kcal > room:
                continue
            protein = sum(o["protein"] for o in combo)
            plans.append({"foods": [o["name"] for o in combo], "kcal": round(kcal), "protein": round(protein),
                          "theirs": sum(1 for o in combo if o["yours"])})
    plans.sort(key=lambda p: (-min(p["protein"], goal), p["kcal"], -p["theirs"], len(p["foods"])))
    chosen = []
    for p in plans:  # different enough from the ones already chosen
        if all(len(set(p["foods"]) & set(c["foods"])) <= 1 for c in chosen):
            chosen.append(p)
        if len(chosen) == most:
            break
    return chosen


def ideas_facts(fit: Fitness) -> dict:
    """What's left today and foods that fit it, worked out here: what the model plans a meal from."""
    s = fit.day_status()
    goals, t, left = s["targets"], s["totals"], s["left"]
    kcal_left, protein_left = left["kcal"], left["protein"]
    tight = kcal_left is not None and kcal_left < LEAN_ROOM  # calories (nearly) used up
    protein_short = protein_left is not None and protein_left > 5
    # With calories used up but protein short: the leanest protein, whatever its calories (it says it goes over)
    options = fit.food_options(None if tight and protein_short else kcal_left, n=10, staples=4)
    if tight and protein_short:
        options = sorted(options, key=lambda o: -o["protein"] / max(o["kcal"], 1))[:4]
    status = []
    if kcal_left is not None:
        status.append(f"calories: {kcal_left:.0f} left" if kcal_left >= 0 else f"calories: {-kcal_left:.0f} OVER the target")
    if protein_left is not None:
        status.append(f"protein: {protein_left:.0f} g still to go" if protein_left > 0 else "protein: target reached")
    big_gap = protein_left is not None and protein_left > 60  # a lot to go: up to 4 foods in a plan
    plans = [] if tight else meal_plans(options, kcal_left, protein_left, items=4 if big_gap else 3)
    if tight and protein_short:
        how = ("Calories are used up but protein is short. Suggest 1 or 2 of the leanest foods below (most protein "
               "for their calories), say how much protein they add and that they take the calories slightly over.")
    elif tight:
        how = "Calories are used up and protein is fine: suggest stopping for today, or only something very light."
    else:
        how = ("Present the best plan (and, briefly, one other) from `plans` exactly as given: each food, then the "
               "plan's total kcal and protein. Their totals are already added up and fit the calories left; never "
               "add numbers up yourself or suggest foods outside the plans.")
    how += " Use the status line as it is; don't recompute it."
    return {"status": "; ".join(status) or "no targets set",
            "eaten_today": {"kcal": round(t.kcal), "protein": round(t.protein)}, "targets": goals,
            "kcal_left": round(kcal_left) if kcal_left is not None else None,
            "protein_left": round(protein_left) if protein_left is not None else None,
            "foods": [{"name": o["name"], "kcal": round(o["kcal"]), "protein": round(o["protein"]),
                       "theirs": o["yours"]} for o in options],
            "plans": plans, "how": how}


def ideas_card(fit: Fitness, text: str | None = None) -> dict:
    """The foods that fit what's left, as a card; `text` is the model's plan, or the app's own short version."""
    f = ideas_facts(fit)
    if text is None:
        kcal_left, protein_left = f["kcal_left"], f["protein_left"]
        short = protein_left is not None and protein_left > 5
        listed = "; ".join(f"{o['name']} ({o['kcal']} kcal, {o['protein']} g)" for o in f["foods"][:3])
        if kcal_left is not None and kcal_left < LEAN_ROOM and short:
            text = (f"You're at your calories ({abs(kcal_left):,} kcal {'over' if kcal_left < 0 else 'left'}) with "
                    f"{protein_left} g protein to go. The leanest ways to close it, taking calories a little over: {listed}.")
        elif kcal_left is not None and kcal_left < LEAN_ROOM:
            text = "You've hit your calories and protein is fine: that's it for today, or something very light."
        else:
            left = ([f"{kcal_left:,} kcal"] if kcal_left is not None else []) + ([f"{protein_left} g protein"] if short else [])
            best = f["plans"][0] if f["plans"] else None
            text = ((f"You have {' and '.join(left)} left today. " if left else "")
                    + (f"One way to get there: {' + '.join(best['foods'])} ({best['kcal']:,} kcal, {best['protein']} g "
                       "protein)." if best else
                       f"Foods that fit, most protein for their calories: {listed}." if listed
                       else "Nothing on my list fits what's left today: something light, like soup or fruit."))
        if not any(f["targets"].values()):
            text += " Set your targets (Fitness page) and I'll fit ideas to them."
    return {"text": text, "kind": "meal_ideas", "payload": {
        "kcal_left": f["kcal_left"], "protein_left": f["protein_left"], "plans": f["plans"],
        "options": [[o["name"], o["kcal"], o["protein"], o["theirs"]] for o in f["foods"]]}}


def answer(fit: Fitness, route) -> dict:
    """The card for one of Dorae-3's lookups (doraemon.router: fit_day, fit_week, fit_review, fit_targets)."""
    if route.intent == "fit_review":
        return review_card(fit, route.slots.get("span", "days:28"))
    if route.intent == "fit_week":
        return week_card(fit, route.slots.get("which", "this"))
    if route.intent == "fit_targets":
        return targets_card(fit)
    day = fit.today() - timedelta(days=1 if route.slots.get("day") == "yesterday" else 0)
    return day_card(fit, day, f"{'Yesterday' if day != fit.today() else 'Today'}, {short_day(day)}")


def food_lines(rows) -> list[str]:
    """A draft or logged meal as plain lines (Telegram)."""
    lines = [f"• {r['name']}" + ((f", {r['portion']}" if "(" in r["portion"] else f" ({r['portion']})") if r["portion"] else "")
             + f": {r['kcal']:.0f} kcal, {r['protein']:.0f} g protein, {r['carbs']:.0f} g carbs, {r['fat']:.0f} g fat"
             + (" · your usual" if r["source"] == "yours" else " · from the label database" if r["source"] == "database"
                else " · from your label photo" if r["source"] == "label" else "")
             for r in rows]
    total_k, total_p = sum(r["kcal"] for r in rows), sum(r["protein"] for r in rows)
    if len(rows) > 1:
        lines.append(f"Total: {total_k:.0f} kcal, {total_p:.0f} g protein")
    return lines
