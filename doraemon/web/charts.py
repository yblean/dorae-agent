"""Geometry for the charts, drawn as inline SVG by the templates (no chart library, works without JS).

Colours follow the category, never its rank, so dining is the same orange in every
chart. Eight categories get their own hue (a colour-blind-checked set); the three
catch-all ones share a grey and are one slice in the donut, but keep their own row
in the list beside it, which always names every category.
"""
import math
from decimal import Decimal

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
