"""Geometry for the charts, drawn as inline SVG by the templates (no chart library, works without JS).

Colours follow the category, never its rank, so dining is the same orange in every
chart. Eight categories get their own hue (a colour-blind-checked set); the three
catch-all ones share a grey and are one slice in the donut, but keep their own row
in the list beside it, which always names every category.
"""
import math
from decimal import Decimal

from doraemon.fitness import KCAL_PER_GRAM

CATEGORY_COLORS = {
    "transport": "#2A78D6", "dining": "#EB6834", "groceries": "#1BAF7A", "travel": "#EDA100",
    "shopping": "#E87BA4", "utilities": "#008300", "subscriptions": "#4A3AA7", "entertainment": "#E34948",
}
NEUTRAL = "#A8A7A0"  # health, memberships, other

DONUT_R = 50
DONUT_C = 2 * math.pi * DONUT_R
GAP = 2  # surface gap between slices, in viewBox units


def color(category: str) -> str:
    return CATEGORY_COLORS.get(category, NEUTRAL)


def money(amount: Decimal, currency: str = "") -> str:
    return f"{currency + ' ' if currency else ''}{amount:,.2f}"


def compact(amount: Decimal) -> str:
    """Short labels for chart caps: 843, 1,204, 12.3k."""
    return f"{amount / 1000:.1f}k" if abs(amount) >= 10000 else f"{amount:,.0f}"


def donut(rows: list[list[str]], currency: str) -> dict:
    """A category breakdown as donut slices plus the list under it ("dining, 30.2% of total")."""
    amounts = [(c, Decimal(a)) for c, a in rows if Decimal(a) > 0]
    whole = sum((a for _, a in amounts), Decimal(0))
    legend = [{"category": c, "color": color(c), "amount": a, "pct": f"{a / whole * 100:.1f}" if whole else "0"}
              for c, a in amounts]
    slices: list[tuple[str, Decimal, list[str]]] = []  # (colour, amount, categories)
    grey = [(c, a) for c, a in amounts if color(c) == NEUTRAL]
    for c, a in amounts:
        if color(c) != NEUTRAL:
            slices.append((color(c), a, [c]))
        elif c == grey[0][0]:  # the grey ones become one slice, where the biggest of them sits
            slices.append((NEUTRAL, sum((g for _, g in grey), Decimal(0)), [g for g, _ in grey]))
    segments, start = [], 0.0
    for col, a, cats in slices:
        length = float(a / whole) * DONUT_C
        dash = length if len(slices) == 1 else max(length - GAP, 0.6)
        names = ", ".join(cats)
        segments.append({"color": col, "dash": f"{dash:.2f} {DONUT_C:.2f}", "offset": f"{-start:.2f}",
                         "tip": f"{names}: {money(a, currency)} ({a / whole * 100:.1f}%)"})
        start += length
    return {"segments": segments, "legend": legend, "total": whole, "r": DONUT_R}


def columns(payload: dict) -> dict:
    """Monthly totals as a column chart. This month is drawn lighter: it isn't finished yet."""
    width, height, top, bottom = 320, 160, 20, 34
    plot = height - top - bottom
    months = payload["months"]
    totals = [Decimal(m["total"]) for m in months]
    budget = Decimal(payload["budget"]) if payload.get("budget") else None
    biggest = max([*totals, budget or Decimal(0), Decimal(1)])
    band = width / max(len(months), 1)
    bar = min(24.0, band * 0.55)
    base = top + plot
    cols = []
    for m, total in zip(months, totals):
        h = float(max(total, Decimal(0)) / biggest) * plot
        x = band * len(cols) + (band - bar) / 2
        y = base - h
        r = min(4.0, h, bar / 2)
        path = (f"M{x:.1f},{base} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} H{x + bar - r:.1f} "
                f"Q{x + bar:.1f},{y:.1f} {x + bar:.1f},{y + r:.1f} V{base} Z") if h > 0 else ""
        tip = f"{m['name']}{' so far' if m['partial'] else ''}: {money(total, payload['currency'])}"
        if budget:
            tip += f" · budget {money(budget, payload['currency'])}"
        cols.append({"path": path, "cx": f"{x + bar / 2:.1f}", "cap_y": f"{y - 6:.1f}", "cap": compact(total),
                     "label": m["label"], "partial": m["partial"], "tip": tip,
                     "band_x": f"{band * len(cols):.1f}", "band_w": f"{band:.1f}"})
    line = None
    if budget:
        line = {"y": f"{base - float(budget / biggest) * plot:.1f}", "label": f"Budget {compact(budget)}"}
    return {"w": width, "h": height, "base": base, "cols": cols, "budget": line,
            "month_y": base + 15, "note_y": base + 27, "top": top}


SERIES = ("#2A78D6", "#EB6834")  # two-series charts: the first two categorical colours (blue, orange)


def line(points: list[dict], currency: str) -> dict:
    """One value over time (net worth at each month's end) as a line with a light wash under it.
    `points`: [{"label", "name", "total", "partial"}]; the last point carries its value as a label."""
    width, height, top, bottom, side = 320, 150, 22, 28, 14
    plot = height - top - bottom
    values = [Decimal(p["total"]) for p in points]
    lo, hi = (min(values), max(values)) if values else (Decimal(0), Decimal(1))
    if lo == hi:
        lo, hi = lo - abs(lo or 1) * Decimal("0.1"), hi + abs(hi or 1) * Decimal("0.1")
    pad = (hi - lo) * Decimal("0.12")
    lo, hi = lo - pad, hi + pad
    step = (width - 2 * side) / max(len(points) - 1, 1)
    x = lambda i: side + i * step if len(points) > 1 else width / 2
    y = lambda v: top + float((hi - v) / (hi - lo)) * plot
    dots = [{"cx": f"{x(i):.1f}", "cy": f"{y(v):.1f}", "label": p["label"], "partial": p["partial"],
             "tip": f"{p['name']}{' so far' if p['partial'] else ''}: {money(v, currency)}",
             "band_x": f"{x(i) - step / 2:.1f}", "band_w": f"{step:.1f}"} for i, (p, v) in enumerate(zip(points, values))]
    path = " ".join(f"{'M' if i == 0 else 'L'}{d['cx']},{d['cy']}" for i, d in enumerate(dots))
    base = top + plot
    area = f"{path} L{dots[-1]['cx']},{base} L{dots[0]['cx']},{base} Z" if dots else ""
    zero = f"{y(Decimal(0)):.1f}" if lo < 0 < hi else None
    return {"w": width, "h": height, "path": path, "area": area, "dots": dots, "base": base, "zero": zero,
            "end": {"x": dots[-1]["cx"], "y": f"{float(dots[-1]['cy']) - 9:.1f}", "text": compact(values[-1])} if dots else None,
            "month_y": base + 16}


def pairs(months: list[dict], currency: str) -> dict:
    """Income against spending per month: two columns side by side, a 2px gap between them.
    `months`: [{"label", "name", "income", "spent", "partial"}]."""
    width, height, top, bottom = 320, 150, 18, 34
    plot = height - top - bottom
    biggest = max([Decimal(m[k]) for m in months for k in ("income", "spent")] + [Decimal(1)])
    band = width / max(len(months), 1)
    bar = min(18.0, band * 0.3)
    base = top + plot
    cols = []
    for i, m in enumerate(months):
        left = band * i + band / 2 - bar - 1
        bars = []
        for j, key in enumerate(("income", "spent")):
            v = max(Decimal(m[key]), Decimal(0))
            h = float(v / biggest) * plot
            bx, by = left + j * (bar + 2), base - h
            r = min(4.0, h, bar / 2)
            path = (f"M{bx:.1f},{base} V{by + r:.1f} Q{bx:.1f},{by:.1f} {bx + r:.1f},{by:.1f} H{bx + bar - r:.1f} "
                    f"Q{bx + bar:.1f},{by:.1f} {bx + bar:.1f},{by + r:.1f} V{base} Z") if h > 0 else ""
            bars.append({"path": path, "color": SERIES[j]})
        kept = Decimal(m["income"]) - Decimal(m["spent"])
        cols.append({"bars": bars, "label": m["label"], "partial": m["partial"], "cx": f"{band * i + band / 2:.1f}",
                     "band_x": f"{band * i:.1f}", "band_w": f"{band:.1f}",
                     "tip": f"{m['name']}{' so far' if m['partial'] else ''}: income {money(Decimal(m['income']), currency)}, "
                            f"spent {money(Decimal(m['spent']), currency)}, kept {money(kept, currency)}"})
    return {"w": width, "h": height, "base": base, "cols": cols, "month_y": base + 15, "note_y": base + 27,
            "colors": SERIES}


# --- Dorae-3: food and steps per day, week or month (Fitness.trend) ------------------------------------------

MACRO_COLORS = {"protein": "#2A78D6", "carbs": "#EB6834", "fat": "#4A3AA7"}  # checked together; not green, Dorae-3's own


def _bar(x: float, y: float, width: float, base: float) -> str:
    """A column from the baseline up to `y`, its top corners rounded."""
    r = min(4.0, base - y, width / 2)
    return (f"M{x:.1f},{base:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} H{x + width - r:.1f} "
            f"Q{x + width:.1f},{y:.1f} {x + width:.1f},{y + r:.1f} V{base:.1f} Z") if base - y > 0 else ""


def _short(v: float) -> str:
    return f"{v / 1000:.1f}k" if v >= 10000 else f"{v:,.0f}"


def _when(b: dict) -> str:
    return f"{b['name']}{' so far' if b['partial'] else ''}"


def _days(n: int, what: str = "logged") -> str:
    return f"{n} day{'s' if n != 1 else ''} {what}"


def fit_columns(trend: dict, key: str, target: int | None, unit: str) -> dict:
    """One measure (kcal, protein or steps) per day, week or month as columns, the target as a reference line. Weeks
    and months show the average a day. Shaped like columns(), so the same macro draws it; with many columns only the
    latest carries its number."""
    width, height, top, bottom = 320, 160, 20, 34
    plot, buckets, daily = height - top - bottom, trend["buckets"], trend["view"] == "days"
    values = [b[key] for b in buckets]
    biggest = max([v or 0 for v in values] + [target or 0, 1])
    band = width / max(len(buckets), 1)
    bar = min(24.0, band * 0.55)
    base = top + plot
    cols = []
    for i, (b, v) in enumerate(zip(buckets, values)):
        x = band * i + (band - bar) / 2
        y = base - (v or 0) / biggest * plot
        counted = b["stepped"] if key == "steps" else b["logged"]
        tip = f"{_when(b)}: " + (f"{v:,.0f}{unit}" + ("" if daily else f" a day ({_days(counted, 'with steps' if key == 'steps' else 'logged')})")
                                 if v is not None else ("no steps" if key == "steps" else "nothing logged"))
        if target:
            tip += f" · target {target:,}{unit}"
        cols.append({"path": _bar(x, y, bar, base), "cx": f"{x + bar / 2:.1f}", "cap_y": f"{y - 6:.1f}",
                     "cap": _short(v) if v is not None and (len(buckets) <= 8 or i == len(buckets) - 1) else "",
                     "label": b["label"], "partial": b["partial"], "tip": tip,
                     "band_x": f"{band * i:.1f}", "band_w": f"{band:.1f}"})
    line = {"y": f"{base - target / biggest * plot:.1f}", "label": f"Target {target:,}{unit}"} if target else None
    return {"w": width, "h": height, "base": base, "cols": cols, "budget": line, "month_y": base + 15,
            "note_y": base + 27, "top": top, "empty": all(v is None for v in values)}


def macro_columns(trend: dict) -> dict:
    """Calories from protein, carbs and fat (4, 4 and 9 kcal a gram) per day, week or month, stacked with protein at
    the bottom: a 2px gap between the parts, the top one rounded."""
    width, height, top, bottom = 320, 160, 14, 34
    plot, buckets, daily = height - top - bottom, trend["buckets"], trend["view"] == "days"
    parts = [{m: (b[m] or 0) * KCAL_PER_GRAM[m] for m in MACRO_COLORS} for b in buckets]
    biggest = max([sum(p.values()) for p in parts] + [1])
    band = width / max(len(buckets), 1)
    bar = min(24.0, band * 0.55)
    base = top + plot
    cols = []
    for i, (b, p) in enumerate(zip(buckets, parts)):
        x, y, segs = band * i + (band - bar) / 2, base, []
        shown = [m for m in MACRO_COLORS if p[m] > 0]
        for m in shown:
            upper = y - p[m] / biggest * plot
            if m == shown[-1]:
                path = _bar(x, upper, bar, y)
            else:  # square, and 2px short of the part above it
                path = f"M{x:.1f},{y:.1f} V{min(upper + GAP, y - 0.5):.1f} H{x + bar:.1f} V{y:.1f} Z"
            segs.append({"path": path, "color": MACRO_COLORS[m]})
            y = upper
        tip = _when(b) + ("" if daily or not b["logged"] else f", a day over {_days(b['logged'])}") + ": " + (
            ", ".join(f"{m} {b[m]:,.0f} g ({p[m]:,.0f} kcal)" for m in MACRO_COLORS) if b["logged"] else "nothing logged")
        cols.append({"bars": segs, "label": b["label"], "partial": b["partial"], "cx": f"{x + bar / 2:.1f}",
                     "band_x": f"{band * i:.1f}", "band_w": f"{band:.1f}", "tip": tip})
    return {"w": width, "h": height, "base": base, "cols": cols, "month_y": base + 15, "note_y": base + 27,
            "colors": MACRO_COLORS}


def macro_split(trend: dict) -> dict:
    """The span's calories split between protein, carbs and fat as donut slices, with grams a day (over the days you
    logged) for the list beside it."""
    whole, days = trend["whole"], trend["logged"]
    kcal = {m: whole[m] * KCAL_PER_GRAM[m] for m in MACRO_COLORS}
    total = sum(kcal.values())
    legend = [{"name": m, "color": c, "grams": whole[m] / days if days else 0,
               "pct": f"{kcal[m] / total * 100:.0f}" if total else "0"} for m, c in MACRO_COLORS.items()]
    shown = [m for m in MACRO_COLORS if kcal[m] > 0]
    segments, start = [], 0.0
    for m in shown:
        length = kcal[m] / total * DONUT_C
        dash = length if len(shown) == 1 else max(length - GAP, 0.6)
        segments.append({"color": MACRO_COLORS[m], "dash": f"{dash:.2f} {DONUT_C:.2f}", "offset": f"{-start:.2f}",
                         "tip": f"{m}: {kcal[m] / total * 100:.0f}% of calories, {whole[m] / days:,.0f} g a day"})
        start += length
    return {"segments": segments, "legend": legend, "r": DONUT_R, "days": days,  # kcal as logged, like the Calories chart
            "kcal": whole["kcal"] / days if days else 0, "sugar": whole["sugar"] / days if days else None}
