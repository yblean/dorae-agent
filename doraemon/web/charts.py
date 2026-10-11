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
