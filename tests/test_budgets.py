"""Budget arithmetic and chart geometry: no database, no model."""
from datetime import date
from decimal import Decimal

from doraemon import budgets as bud
from doraemon.web import charts


def status(spent, limit="300", day=10, days=30):
    return bud.Status("dining", Decimal(limit), Decimal(spent), day, days)


def test_states_follow_the_pace():
    assert status("50").state == "on_track"           # 17% used, a third of the month gone
    at_risk = status("150")                           # half used after a third: heads for 450
    assert at_risk.state == "at_risk" and at_risk.projected == Decimal("450.00")
    assert at_risk.per_day == Decimal("7.50")         # 150 left over 20 days
    over = status("320")
    assert over.state == "over" and over.per_day is None and over.used == 106


def test_describe_says_what_keeps_you_within():
    assert bud.describe(status("150"), "SGD") == ("Dining: SGD 150.00 of SGD 300.00 used (50%), 20 days left. "
                                                  "At this pace it reaches SGD 450.00: keep to SGD 7.50 a day to stay within.")
    assert bud.describe(status("320"), "SGD").endswith("SGD 20.00 over.")


def test_past_months_count_as_finished():
    [s] = bud.statuses({"total": Decimal("100")}, {}, Decimal("120"), "2026-09", date(2026, 10, 10))
    assert s.day == 30 and s.days_left == 0 and s.state == "over"


def test_overall_budget_comes_first():
    stats = bud.statuses({"dining": Decimal(1), "total": Decimal(2)}, {}, Decimal(0), "2026-10", date(2026, 10, 1))
    assert [s.category for s in stats] == ["total", "dining"]


def test_parse_request():
    assert bud.parse_request("set my dining budget to $300") == ("dining", Decimal("300.00"))
    assert bud.parse_request("I want a monthly budget of 1.5k") == ("total", Decimal("1500.00"))
    assert bud.parse_request("change food budget to 250") == ("dining", Decimal("250.00"))
    assert bud.parse_request("how am I doing on my budget?") is None
    assert bud.parse_request("set a budget") is None


def test_alerts_once_per_level():
    s = status("250")  # 83%
    assert bud.new_alerts([s], {}) == [(s, 80)]
    assert bud.new_alerts([s], {"dining": 80}) == []
    assert bud.new_alerts([status("301")], {"dining": 80})[0][1] == 100


def test_donut_merges_the_grey_categories_but_lists_each():
    d = charts.donut([["dining", "60"], ["other", "30"], ["health", "10"], ["shopping", "-5"]], "SGD")
    assert [l["category"] for l in d["legend"]] == ["dining", "other", "health"]  # net refunds left out
    assert len(d["segments"]) == 2 and "other, health" in d["segments"][1]["tip"]
    assert d["legend"][0]["pct"] == "60.0"


def test_columns_scale_to_the_budget():
    c = charts.columns({"currency": "SGD", "budget": "200", "months": [
        {"month": "2026-09", "label": "Sep", "name": "September", "total": "100", "partial": False},
        {"month": "2026-10", "label": "Oct", "name": "October", "total": "0", "partial": True}]})
    assert c["budget"]["y"] == f"{c['top']:.1f}"  # the budget is the tallest thing
    assert c["cols"][1]["path"] == "" and c["cols"][1]["partial"]
