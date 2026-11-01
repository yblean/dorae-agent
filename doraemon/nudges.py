"""Dorae-3 speaking first, from what the other agents already know, with buttons so acting on it is one tap.

- meal_prompt(): a payment at a food place (Dorae-1 read it from your bank alert) -> "Lunch at Kopitiam? Chicken rice
  again?" with the foods you logged after your last payment there as a ready draft. Partly logged days are the main
  gap in your numbers; this closes them while you still remember what you had.
- breakfast_prompt(): your usual breakfast (the same first meal on several recent mornings) as a draft at 9:30,
  when nothing is logged yet.
- meal_out_note(): a lunch or dinner out on your calendar (Dorae-2's) today -> how much is left for it, so the rest
  of the day can make room.
- weekly(): last week's review, plus changes to your targets when the same gap shows in the last four weeks too.
  You press a button to make one; nothing changes without you.

Every number is worked out here from your log; no model. The jobs (doraemon.jobs) decide when, and send them.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from doraemon import coach
from doraemon.fitness import Fitness, key_of

Buttons = list[list[tuple[str, str]]]

PROMPT_HOURS = 4          # a payment older than this when it arrives is too late to ask about
PROMPT_GAP = timedelta(hours=2)  # at most one meal question in this long (twin alerts, a drink after lunch)
USUAL_WINDOW = timedelta(hours=3)  # food logged this soon after a payment is what you had there
BREAKFAST_AT = (9, 30)
LUNCH_NOTE_AT, DINNER_NOTE_AT = (10, 0), (15, 0)
# A meal out on your calendar: "Dinner with Alex", "Team lunch", "Steamboat at Jen's", "Drinks"
# Payment processors standing in for the real shop ("STRIPE PAYMENTS SINGAPORE", "SQ *CAFE"): the place is unknown
PROCESSOR = re.compile(r"\b(?:stripe|paypal|nets|2c2p|fomo ?pay|adyen|ipay88|razer ?pay|grab ?pay|shopee ?pay|"
                       r"atome)\b|^sq \*", re.I)
MEAL_OUT = re.compile(r"\b(?:breakfast|brunch|lunch|dinner|supper|buffet|bbq|barbecue|steamboat|hot ?pot|omakase|"
                      r"high tea|makan|feast|potluck|drinks|restaurant)\b", re.I)


@dataclass
class Nudge:
    text: str
    buttons: Buttons | None = None
    ask: dict | None = None  # what a plain reply answers (doraemon.phone's ASK_KEY), e.g. {"kind": "meal", ...}


def meal_name(at: datetime) -> str:
    hour = at.hour + at.minute / 60
    return ("Breakfast" if hour < 10.5 else "Lunch" if hour < 15 else "A snack" if hour < 17.5
            else "Dinner" if hour < 21.5 else "Supper")


def merchant_name(merchant: str) -> str:
    """'MOS BURGER-JURONG POINT SINGAPORE SG' -> 'Mos Burger', 'McDonalds 930013 Singapore SG' -> 'Mcdonalds'."""
    words = [w for w in re.split(r"[\s\-@*,/]+", merchant) if w and not re.search(r"\d", w)]
    words = [w for w in words if w.upper() not in ("SG", "SGP", "SINGAPORE", "PTE", "LTD", "LTD.", "PTE.")]
    return " ".join(w.capitalize() for w in words[:2]) or merchant[:30]


def same_place(a: str, b: str) -> bool:
    """Two payments at the same place: the same first word, or first two when it's short ("McDonald's Jurong Bowl" ~
    "McDonalds 930013", "MOS BURGER-JURONG" ~ "Mos Burger Bugis"); numbers (store codes) don't count."""
    def words(m):
        found = [w for w in key_of(re.sub(r"'", "", m)).split() if not w.isdigit()]
        return found[:1] if found and len(found[0]) >= 5 else found[:2]
    return bool(words(a)) and words(a) == words(b)


def foods_after(fit: Fitness, at: datetime) -> list:
    """The first food batch logged within USUAL_WINDOW after `at`: what you had there."""
    rows = fit.db.conn.execute("SELECT * FROM food_log WHERE status = 'logged' AND day = ? ORDER BY id",
                               (at.astimezone(fit.tz).date().isoformat(),)).fetchall()
    after = [r for r in rows if timedelta(0) <= datetime.fromisoformat(r["logged_at"]) - at <= USUAL_WINDOW]
    return [r for r in after if r["batch"] == after[0]["batch"]] if after else []


def usual_at(fit: Fitness, merchant: str, before: datetime) -> list:
    """What you logged after your latest earlier payment at the same place, or [] if nothing was."""
    for t in fit.db.conn.execute("SELECT merchant, purchased_at FROM transactions WHERE status = 'counted' "
                                 "AND is_refund = 0 AND category = 'dining' ORDER BY purchased_at DESC LIMIT 300"):
        at = datetime.fromisoformat(t["purchased_at"])
        if at < before - timedelta(minutes=5) and same_place(t["merchant"], merchant) and (rows := foods_after(fit, at)):
            return rows
    return []


def copy(rows) -> list[dict]:
    """Logged foods as a new draft's foods: your numbers, as you logged them."""
    return [{"name": r["name"], "portion": r["portion"], **{m: r[m] for m in ("kcal", "protein", "carbs", "fat", "sugar")},
             "source": "yours"} for r in rows]


def meal_prompt(fit: Fitness, txn, now: datetime) -> Nudge | None:
    """The question about one new payment at a food place, or None when it isn't worth asking: a refund, too old,
    at night, or food already logged since it."""
    at = datetime.fromisoformat(txn["purchased_at"]).astimezone(fit.tz)
    if txn["is_refund"] or txn["category"] != "dining" or txn["status"] != "counted" \
            or not timedelta(0) <= now - at <= timedelta(hours=PROMPT_HOURS) or not 6 <= at.hour < 23:
        return None
    if foods_after(fit, at - timedelta(minutes=30)):  # you've logged it already
        return None
    meal, day, unknown = meal_name(at), at.date(), bool(PROCESSOR.search(txn["merchant"]))
    place = "" if unknown else merchant_name(txn["merchant"])
    when = f"{at:%I:%M%p}".lstrip("0").lower()
    head = f"🍽 {meal}{f' at {place}' if place else ''}? ({txn['currency']} {txn['amount']} at {when})"
    ask = {"kind": "meal", "title": place, "day": day.isoformat(), "minutes": 180}
    usual = [] if unknown else usual_at(fit, txn["merchant"], at)  # a processor's payments are anywhere
    if not usual:
        return Nudge(f"{head}\nWhat did you have? Reply like “chicken rice and kopi” and I'll log it.",
                     [[("✖ Skip", "fskip:0")]], ask)
    batch = fit.draft(copy(usual), day)
    names = " + ".join(r["name"] for r in usual)
    kcal, protein = sum(r["kcal"] for r in usual), sum(r["protein"] for r in usual)
    return Nudge(f"{head}\nLast time there: {names} ({kcal:,.0f} kcal, {protein:.0f} g protein). Same again?\n"
                 "Or reply with what you had.",
                 [[(f"✅ {names[:28]} again", f"flog:{batch}")], [("✏️ Something else", f"fmeal:{batch}"),
                                                                  ("✖ Skip", f"fdrop:{batch}")]], ask)


def usual_breakfast(fit: Fitness, today: date) -> list:
    """Your usual first meal: the same foods first logged before 11am on at least 3 of the last 14 mornings, one of
    them in the last week. The latest of those, or []."""
    mornings: dict[str, list] = {}
    for back in range(1, 15):
        day = today - timedelta(days=back)
        rows = fit.db.conn.execute("SELECT * FROM food_log WHERE status = 'logged' AND day = ? ORDER BY logged_at",
                                   (day.isoformat(),)).fetchall()
        if not rows or datetime.fromisoformat(rows[0]["logged_at"]).astimezone(fit.tz).hour >= 11:
            continue
        first = [r for r in rows if r["batch"] == rows[0]["batch"]]
        mornings.setdefault(" + ".join(sorted(key_of(r["name"]) for r in first)), []).append((back, first))
    seen = [m for m in mornings.values() if len(m) >= 3 and min(b for b, _ in m) <= 7]
    return max(seen, key=len)[0][1] if seen else []


def breakfast_prompt(fit: Fitness, now: datetime) -> Nudge | None:
    today = now.date()
    if fit.foods_on(today) or not (usual := usual_breakfast(fit, today)):
        return None
    batch = fit.draft(copy(usual), today)
    names = " + ".join(r["name"] for r in usual)
    return Nudge(f"☀️ {names} again this morning? ({sum(r['kcal'] for r in usual):,.0f} kcal, "
                 f"{sum(r['protein'] for r in usual):.0f} g protein)",
                 [[("✅ Log it", f"flog:{batch}"), ("✖ Not today", f"fdrop:{batch}")]])


# "same as yesterday", "had the same lunch as yesterday", "my usual breakfast", "the usual", "same dinner as last night"
REPEAT = re.compile(r"^\W*(?:(?:i\s+)?(?:just\s+)?(?:had|ate|have|having|log|add)\s+)?(?:the\s+|my\s+)?"
                    r"(?:(?P<usual>usual)|same(?:\s+(?:thing|food|meal|one))?)(?:\s+(?P<meal>breakfast|brunch|lunch|dinner|"
                    r"supper))?(?:\s+(?:as|like)\s+(?P<when>yesterday|last night))?(?:\s+(?:again|today|please|pls))*\W*$",
                    re.I)
MEAL_HOURS = {"breakfast": (0, 11), "brunch": (0, 14), "lunch": (11, 16), "dinner": (16, 24), "supper": (20, 24)}


def repeat_choices(fit: Fitness, text: str, today: date) -> tuple[str, list[list], bool] | None:
    """A meal to log again: (what it is, the batches it could be, whether it's surely the one), or None if `text`
    doesn't ask for one. For a meal you named, yesterday's batches are narrowed by when they were logged; one logged
    at another time (a whole day logged at night) isn't surely that meal, so you choose."""
    if not (m := REPEAT.match(text)):
        return None
    meal = (m["meal"] or ("dinner" if m["when"] == "last night" else "")).lower()
    if m["usual"] and meal in ("", "breakfast", "brunch") and (usual := usual_breakfast(fit, today)):
        return "usual breakfast", [usual], True
    rows = fit.foods_on(today - timedelta(days=1))
    batches = list({r["batch"]: [x for x in rows if x["batch"] == r["batch"]] for r in rows}.values())
    if not meal:
        return "yesterday", batches, len(batches) == 1
    lo, hi = MEAL_HOURS[meal]
    timed = [b for b in batches if lo <= datetime.fromisoformat(b[0]["logged_at"]).astimezone(fit.tz).hour < hi]
    return (f"usual {meal}" if m["usual"] else f"{meal} as yesterday"), (timed if len(timed) == 1 else batches), \
        len(timed) == 1


def meals_out(events: list[dict], today: date, lunch: bool) -> list[dict]:
    """Today's timed calendar events that look like a meal out: lunch ones (before 3pm) or the rest."""
    found = []
    for e in events:
        if e["first"] != today.isoformat() or not e.get("start") or not MEAL_OUT.search(e["title"]):
            continue
        if (int(e["start"][:2]) < 15) == lunch:
            found.append(e)
    return found


def meal_out_note(fit: Fitness, event: dict) -> Nudge:
    """How much is left for a meal out today, and how to make room for it."""
    s = fit.day_status()
    goals, t = s["targets"], s["totals"]
    at = datetime.strptime(event["start"], "%H:%M")
    when = f"{at:%I:%M%p}".lstrip("0").lower().replace(":00", "")
    head = f"🍽 {event['title']} at {when} today" + (f" ({event['location']})" if event.get("location") else "") + "."
    if not goals["kcal"]:
        return Nudge(f"{head} Tell me what you have afterwards and I'll log it.")
    left = goals["kcal"] - t.kcal
    text = f"{head} So far today: {t.kcal:,.0f} kcal" + (f", {t.protein:.0f} g protein" if t.foods else "") + "."
    if left <= 300:
        return Nudge(f"{text} You're close to your {goals['kcal']:,} kcal already, so go easy: share dishes or skip "
                     "dessert, and tomorrow can balance it out.")
    meal = "lunch" if int(event["start"][:2]) < 15 else "dinner"
    snack = fit.protein_ideas(s["left"]["protein"] or 20, 200, n=1) if goals["protein"] else []
    return Nudge(f"{text} That leaves about {left:,.0f} kcal for the rest of the day, so keep things light before "
                 f"{meal}" + (f": if you're hungry, {snack[0]} fits." if snack else ".")
                 + " Tell me what you have afterwards and I'll log it.")


# --- the weekly review and changes to your targets -------------------------------------------------------------

def round_to(value: float, step: int) -> int:
    return int(round(value / step) * step)


def proposals(fit: Fitness) -> list[dict]:
    """Target changes worth offering: a gap that shows both last week (at least 3 fully logged days) and over the
    last four weeks, so one odd week isn't enough. [{"key", "to", "text"}], at most 2, within sensible limits."""
    goals = fit.targets()
    first, last, _ = coach.review_span("week:last", fit.today())
    week = fit.review(first, last)
    first, last, _ = coach.review_span("days:28", fit.today())
    month = fit.review(first, last)
    out = []
    if week["full"] >= 3:
        if goals["protein"] and week["protein"] < goals["protein"] * 0.9 and month["protein"] < goals["protein"] * 0.95:
            floor = 0.8 * float(fit.profile().get("weight_kg") or 0)
            to = max(round_to(month["protein"], 5), round_to(floor, 5), coach.SENSIBLE["protein"][0])
            ideas = fit.protein_ideas(goals["protein"] - month["protein"], None, n=1)
            if to < goals["protein"]:
                out.append({"key": "protein", "to": to, "text": (
                    f"Protein has been short for a while: {week['protein']:.0f} g a day last week, {month['protein']:.0f} g "
                    f"over four weeks, against {goals['protein']} g. Keep {goals['protein']} g and add "
                    + (ideas[0] if ideas else "a protein snack") + f" on most days, or set {to} g, a target you hit now.")})
        if goals["kcal"] and week["kcal"] > goals["kcal"] * 1.1 and month["kcal"] > goals["kcal"] * 1.05:
            to = round_to(month["kcal"], 50)
            out.append({"key": "kcal", "to": to, "text": (
                f"Calories have been over: {week['kcal']:,.0f} a day last week, {month['kcal']:,.0f} over four weeks, "
                f"against {goals['kcal']:,}. Keep {goals['kcal']:,} and trim the biggest meals, or set {to:,} if that's "
                "what suits you" + (" (your goal is to lose weight, so keeping it is the way there)."
                                    if fit.profile().get("goal") == "lose" else "."))})
        elif goals["kcal"] and fit.profile().get("goal") != "lose" and week["kcal"] < goals["kcal"] * 0.85 \
                and month["kcal"] < goals["kcal"] * 0.9:
            to = max(round_to(month["kcal"], 50), coach.SENSIBLE["kcal"][0])
            out.append({"key": "kcal", "to": to, "text": (
                f"You've been eating under your target: {week['kcal']:,.0f} a day last week, {month['kcal']:,.0f} over four "
                f"weeks, against {goals['kcal']:,}. Keep it and add a snack, or set {to:,} if that's what suits you.")})
    if goals["steps"] and week["stepped"] >= 4 and week["steps"] < goals["steps"] * 0.7:
        to = max(round_to(week["steps"] * 1.1, 500), coach.SENSIBLE["steps"][0])
        out.append({"key": "steps", "to": to, "text": (
            f"Steps averaged {week['steps']:,.0f} last week against {goals['steps']:,}. A target you can hit builds the "
            f"habit: set {to:,} for now and raise it later, or keep {goals['steps']:,}.")})
    return out[:2]


UNITS = {"kcal": " kcal", "protein": " g", "steps": " steps"}


def proposal_buttons(found: list[dict], goals: dict) -> Buttons:
    return [[(f"✅ Set {p['to']:,}{UNITS[p['key']]}", f"fgoal:{p['key']}:{p['to']}"),
             (f"Keep {goals[p['key']]:,}{UNITS[p['key']]}", f"fkeep:{p['key']}")] for p in found]


def weekly(fit: Fitness) -> tuple[dict, list[dict]]:
    """Last week's review card and the target changes worth offering with it."""
    return coach.review_card(fit, "week:last"), proposals(fit)
