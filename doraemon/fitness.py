"""Dorae-3: what you eat, your steps and your daily targets.

- Targets: calories, protein and steps a day, set by you or suggested from a short profile
  (suggest_targets: Mifflin-St Jeor for energy, protein per kg of body weight).
- Food: a message ("chicken rice and kopi siew dai") becomes a draft of foods with their calories and
  macros (doraemon.foodlog works them out), waiting for Log. Logged foods teach the library, so your
  usual orders come back with your own numbers.
- Steps: read from a folder your iPhone's Shortcuts automation saves Apple Health counts to
  (import_steps); the highest count for a day wins, so an early run never undoes a later one.
- The evening check (evening_check): what's left for the day and how to close the gap, from foods
  you actually eat. Every number is worked out here; nothing is invented by a model.
"""
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from doraemon.db import Database

MACROS = ("kcal", "protein", "carbs", "fat", "sugar")
TARGETS_KEY, PROFILE_KEY = "fitness:targets", "fitness:profile"
TARGET_NAMES = {"kcal": "calories", "protein": "protein", "steps": "steps"}
UNITS = {"kcal": "kcal", "protein": "g", "carbs": "g", "fat": "g", "sugar": "g", "steps": ""}
STEPS_PER_MINUTE = 100  # a brisk walk
ACTIVITY = {"low": 1.2, "light": 1.375, "moderate": 1.55, "high": 1.725}  # TDEE multipliers
GOALS = {"lose": -450, "maintain": 0, "gain": 300}  # kcal a day against what you burn
# Typical protein snacks, for the evening plan when you haven't logged enough foods of your own yet
STAPLES = [
    {"name": "2 hard-boiled eggs", "kcal": 140, "protein": 12},
    {"name": "Greek yogurt (150 g)", "kcal": 130, "protein": 15},
    {"name": "Unsweetened soy milk (250 ml)", "kcal": 95, "protein": 9},
    {"name": "Chicken breast (150 g)", "kcal": 250, "protein": 45},
    {"name": "Firm tofu (200 g)", "kcal": 150, "protein": 16},
    {"name": "Tuna in water (1 can)", "kcal": 120, "protein": 26},
]


def key_of(name: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", name.lower()))


def r1(x) -> float:
    return round(float(x or 0), 1)


@dataclass
class Totals:
    kcal: float = 0
    protein: float = 0
    carbs: float = 0
    fat: float = 0
    sugar: float = 0
    foods: int = 0


def suggest_targets(profile: dict) -> dict:
    """Daily targets from {"weight_kg", "height_cm", "age", "sex": male/female, "activity": low/light/moderate/high,
    "goal": lose/maintain/gain}: energy by Mifflin-St Jeor times activity, adjusted for the goal; protein 1.8 g per
    kg (2.0 when losing, to keep muscle); 8,000 steps (10,000 if you're already active)."""
    w, h, age = float(profile["weight_kg"]), float(profile["height_cm"]), float(profile["age"])
    bmr = 10 * w + 6.25 * h - 5 * age + (5 if profile.get("sex") == "male" else -161)
    burn = bmr * ACTIVITY.get(profile.get("activity", "light"), 1.375)
    goal = profile.get("goal", "maintain")
    kcal = max(1200 if profile.get("sex") != "male" else 1500, burn + GOALS.get(goal, 0))
    return {"kcal": int(round(kcal / 50) * 50), "protein": int(round(w * (2.0 if goal == "lose" else 1.8) / 5) * 5),
            "steps": 10000 if profile.get("activity") in ("moderate", "high") else 8000}


class Fitness:
    def __init__(self, db: Database, user_tz: str) -> None:
        self.db, self.tz = db, ZoneInfo(user_tz)

    def today(self) -> date:
        return datetime.now(self.tz).date()

    # --- targets and profile ---------------------------------------------------------------------

    def targets(self) -> dict:
        """{"kcal", "protein", "steps"}: each a number, or None when not set."""
        saved = json.loads(self.db.get_setting(TARGETS_KEY) or "{}")
        return {k: saved.get(k) for k in TARGET_NAMES}

    def set_targets(self, **targets) -> dict:
        current = self.targets()
        for k, v in targets.items():
            if k in TARGET_NAMES:
                current[k] = int(v) if v not in (None, "", 0) else None
        self.db.set_setting(TARGETS_KEY, json.dumps(current))
        return current

    def profile(self) -> dict:
        return json.loads(self.db.get_setting(PROFILE_KEY) or "{}")

    def set_profile(self, profile: dict) -> None:
        self.db.set_setting(PROFILE_KEY, json.dumps(profile))

    # --- the food log ------------------------------------------------------------------------------

    def draft(self, foods: list[dict], day: date | None = None) -> str:
        """Save foods ([{"name", "portion", "kcal", "protein", "carbs", "fat", "sugar", "source", "note"}]) as a draft
        waiting for Log. Returns the batch id."""
        batch = uuid.uuid4().hex[:8]
        day = (day or self.today()).isoformat()
        for f in foods:
            self.db.conn.execute(
                """INSERT INTO food_log (day, logged_at, name, portion, kcal, protein, carbs, fat, sugar, source,
                   status, batch, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
                (day, datetime.now(timezone.utc).isoformat(), f["name"][:80], (f.get("portion") or "")[:40],
                 *(r1(f.get(m)) for m in MACROS), f.get("source", "estimate"), batch, (f.get("note") or "")[:120]))
        self.db.conn.commit()
        return batch

    def batch(self, batch: str):
        return self.db.conn.execute("SELECT * FROM food_log WHERE batch = ? ORDER BY id", (batch,)).fetchall()

    def log(self, batch: str) -> list:
        """Log a draft: it counts, and each food is remembered (with your numbers) for next time."""
        rows = [r for r in self.batch(batch) if r["status"] == "draft"]
        for r in rows:
            self.db.conn.execute("UPDATE food_log SET status = 'logged' WHERE id = ?", (r["id"],))
            self.remember(r)
        self.db.conn.commit()
        return rows

    def cancel(self, batch: str) -> int:
        cur = self.db.conn.execute("UPDATE food_log SET status = 'cancelled' WHERE batch = ? AND status = 'draft'",
                                   (batch,))
        self.db.conn.commit()
        return cur.rowcount

    def edit(self, food_id: int, **changes) -> None:
        """Change a food's name, portion or numbers (a draft, or one already logged: then the library learns it)."""
        fields = {k: (r1(v) if k in MACROS else str(v)[:80]) for k, v in changes.items()
                  if k in (*MACROS, "name", "portion") and v is not None}
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.conn.execute(f"UPDATE food_log SET {sets}, source = 'you' WHERE id = ?", [*fields.values(), food_id])
        self.db.conn.commit()
        row = self.db.conn.execute("SELECT * FROM food_log WHERE id = ?", (food_id,)).fetchone()
        if row and row["status"] == "logged":
            self.remember(row)

    def remove(self, food_id: int) -> None:
        self.db.conn.execute("UPDATE food_log SET status = 'cancelled' WHERE id = ?", (food_id,))
        self.db.conn.commit()

    def foods_on(self, day: date):
        return self.db.conn.execute("SELECT * FROM food_log WHERE day = ? AND status = 'logged' ORDER BY id",
                                    (day.isoformat(),)).fetchall()

    def totals(self, day: date) -> Totals:
        t = Totals()
        for r in self.foods_on(day):
            for m in MACROS:
                setattr(t, m, getattr(t, m) + r[m])
            t.foods += 1
        return t

    # --- the library: your foods with your numbers ----------------------------------------------------

    def remember(self, row) -> None:
        self.db.conn.execute(
            """INSERT INTO foods (key, name, portion, kcal, protein, carbs, fat, sugar, uses, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT (key) DO UPDATE SET name = excluded.name, portion = excluded.portion, kcal = excluded.kcal,
                 protein = excluded.protein, carbs = excluded.carbs, fat = excluded.fat, sugar = excluded.sugar,
                 uses = foods.uses + 1, updated_at = excluded.updated_at""",
            (key_of(row["name"]), row["name"], row["portion"], *(row[m] for m in MACROS),
             datetime.now(timezone.utc).isoformat()))

    def known(self, name: str):
        """Your own numbers for a food you've logged before, or None."""
        return self.db.conn.execute("SELECT * FROM foods WHERE key = ?", (key_of(name),)).fetchone()

    def library(self, limit: int = 200):
        return self.db.conn.execute("SELECT * FROM foods ORDER BY uses DESC, updated_at DESC LIMIT ?", (limit,)).fetchall()

    # --- steps --------------------------------------------------------------------------------------

    def set_steps(self, day: date, steps: int, active_kcal: float | None = None, source: str = "apple health") -> bool:
        """Save a day's steps; the highest count for the day wins. Returns whether it changed."""
        old = self.steps_on(day)
        if old is not None and old >= steps:
            return False
        self.db.conn.execute(
            """INSERT INTO steps (day, steps, active_kcal, source, updated_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (day) DO UPDATE SET steps = excluded.steps,
                 active_kcal = COALESCE(excluded.active_kcal, steps.active_kcal), source = excluded.source,
                 updated_at = excluded.updated_at""",
            (day.isoformat(), int(steps), active_kcal, source, datetime.now(timezone.utc).isoformat()))
        self.db.conn.commit()
        return True

    def steps_on(self, day: date) -> int | None:
        row = self.db.conn.execute("SELECT steps FROM steps WHERE day = ?", (day.isoformat(),)).fetchone()
        return row["steps"] if row else None

    # --- days and weeks -------------------------------------------------------------------------------

    def day_status(self, day: date | None = None) -> dict:
        """Everything about one day against your targets, for answers, the panel and the evening check."""
        day = day or self.today()
        t, goals, steps = self.totals(day), self.targets(), self.steps_on(day)
        got = {"kcal": t.kcal, "protein": t.protein, "steps": steps}
        return {"day": day, "totals": t, "steps": steps, "targets": goals,
                "left": {k: (goals[k] - got[k]) if goals[k] and got[k] is not None else None for k in TARGET_NAMES},
                "foods": self.foods_on(day)}

    def days(self, first: date, last: date) -> list[dict]:
        out, d = [], first
        while d <= last:
            t = self.totals(d)
            out.append({"day": d, "kcal": t.kcal, "protein": t.protein, "foods": t.foods, "steps": self.steps_on(d)})
            d += timedelta(days=1)
        return out

    def week_facts(self, end: date | None = None, days: int = 7) -> dict:
        """The last `days` days (today included) worked out: averages over the days you logged, how many days hit
        each target, best and worst. What the model gets for advice, and the week answer."""
        end = end or self.today()
        rows = self.days(end - timedelta(days=days - 1), end)
        goals = self.targets()
        logged = [r for r in rows if r["foods"]]
        stepped = [r for r in rows if r["steps"] is not None]
        avg = lambda xs: round(sum(xs) / len(xs)) if xs else None
        facts = {"days": days, "days_logged": len(logged), "targets": goals,
                 "avg_kcal": avg([r["kcal"] for r in logged]), "avg_protein": avg([r["protein"] for r in logged]),
                 "avg_steps": avg([r["steps"] for r in stepped]), "rows": rows}
        if goals["kcal"] and logged:
            facts["days_within_kcal"] = sum(1 for r in logged if r["kcal"] <= goals["kcal"] * 1.05)
        if goals["protein"] and logged:
            facts["days_protein_hit"] = sum(1 for r in logged if r["protein"] >= goals["protein"] * 0.95)
        if goals["steps"] and stepped:
            facts["days_steps_hit"] = sum(1 for r in stepped if r["steps"] >= goals["steps"])
        return facts

    # --- the evening check -------------------------------------------------------------------------------

    def evening_check(self, day: date | None = None) -> str | None:
        """What's left today and how to close the gap: protein from foods you eat that fit the calories left, steps as
        minutes of walking, a heads-up if calories are already over. None when you're on track (or nothing's set)."""
        s = self.day_status(day)
        goals, t, left = s["targets"], s["totals"], s["left"]
        lines = []
        kcal_left = left["kcal"]
        if goals["protein"] and left["protein"] is not None and left["protein"] > 15:
            ideas = self.protein_ideas(left["protein"], kcal_left)
            lines.append(f"🥚 Protein: {t.protein:.0f} of {goals['protein']} g, {left['protein']:.0f} g to go."
                         + (" Fits your calories: " + "; ".join(ideas) + "." if ideas else ""))
        if goals["steps"] and s["steps"] is not None and left["steps"] and left["steps"] > 500:
            minutes = round(left["steps"] / STEPS_PER_MINUTE / 5) * 5 or 5
            lines.append(f"🚶 Steps: {s['steps']:,} of {goals['steps']:,}. A {minutes}-minute walk gets you there.")
        if goals["kcal"] and kcal_left is not None and kcal_left < -100:
            lines.append(f"🍽 Calories: {t.kcal:.0f} of {goals['kcal']}, {-kcal_left:.0f} over. A lighter dinner (or "
                         "a longer walk) keeps the week on track.")
        elif goals["kcal"] and t.foods and kcal_left is not None and kcal_left > 0 and lines:
            lines.append(f"🍽 {kcal_left:.0f} kcal left for today.")
        if not lines:
            return None
        return "\n".join([f"⏰ Your 6pm check for {s['day']:%a %d %b}:".replace(" 0", " ")] + lines)

    def protein_ideas(self, protein_left: float, kcal_left: float | None, n: int = 2) -> list[str]:
        """Foods with plenty of protein for their calories that fit what's left: yours first, then common staples."""
        room = kcal_left if kcal_left is not None else 9999
        mine = [{"name": r["name"], "kcal": r["kcal"], "protein": r["protein"]} for r in self.library()
                if r["protein"] >= 8 and 0 < r["kcal"] <= room]
        pool = sorted(mine, key=lambda f: -f["protein"] / f["kcal"])[:n]
        if len(pool) < n:
            pool += [f for f in STAPLES if f["kcal"] <= room and f["name"] not in {p["name"] for p in pool}][:n - len(pool)]
        return [f"{f['name']} ({f['kcal']:.0f} kcal, {f['protein']:.0f} g)" for f in pool]


# --- steps from Apple Health (a Shortcuts automation saving to a OneDrive folder) ---------------------------

_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})|(\d{1,2})/(\d{1,2})/(\d{4})")
# a number: "8,423" or "8 423" (a comma or space before exactly three digits separates thousands), "8423", "412.5"
_NUM = r"\d{1,3}(?:[, ]\d{3})+(?![\d])(?:\.\d+)?|\d+(?:\.\d+)?"
_STEPS = re.compile(rf"steps?\D{{0,3}}?({_NUM})", re.I)
_ACTIVE = re.compile(rf"(?:active|energy|kcal|cal(?:orie)?s?)\D{{0,3}}?({_NUM})", re.I)


def _number(text: str) -> float:
    return float(text.replace(",", "").replace(" ", ""))


def parse_steps(text: str, fallback_day: date | None = None) -> list[tuple[date, int, float | None]]:
    """(day, steps, active kcal) from what the Shortcut saves. Best labelled ('2026-10-19 steps 8423 active 412'), but
    '2026-10-19,8423,412' works too, as do thousands commas ('8,423'), decimals and day-first dates. A line without a
    date uses `fallback_day` (from the file's name)."""
    found = []
    for line in text.splitlines():
        line = re.sub(r"[\u00a0\u202f]", " ", line.strip())  # no-break spaces in numbers
        m = _DATE.search(line)
        if m:
            day = date(int(m[1]), int(m[2]), int(m[3])) if m[1] else date(int(m[6]), int(m[5]), int(m[4]))
            rest = line[:m.start()] + " " + line[m.end():]
        elif fallback_day and line:
            day, rest = fallback_day, line
        else:
            continue
        steps, active = _STEPS.search(rest), _ACTIVE.search(rest)
        if steps:
            found.append((day, int(_number(steps[1])), _number(active[1]) if active else None))
            continue
        nums = [_number(n) for n in re.findall(_NUM, rest)]
        if nums:
            found.append((day, int(nums[0]), nums[1] if len(nums) > 1 else None))
    return found


def health_folders(setting: str) -> list[Path]:
    """The folders DORAEMON_HEALTH_FOLDER names (separated by ";"), whether they exist yet or not."""
    return [Path(p.strip()) for p in setting.split(";") if p.strip()]


def import_steps(fit: Fitness, folder: str | Path) -> int:
    """Read the step files in `folder` (or in each of several separated by ";"). Returns how many days changed."""
    if isinstance(folder, str) and ";" in folder:
        return sum(import_steps(fit, f) for f in health_folders(folder))
    folder = Path(folder)
    if not folder.is_dir():
        return 0
    changed = 0
    for path in sorted(folder.glob("*")):
        if path.suffix.lower() not in (".txt", ".csv", "") or not path.is_file():
            continue
        m = _DATE.search(path.stem)
        named = (date(int(m[1]), int(m[2]), int(m[3])) if m and m[1] else None)
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for day, steps, active in parse_steps(text, named):
            if 0 <= steps < 200000 and fit.set_steps(day, steps, active):
                changed += 1
    return changed
