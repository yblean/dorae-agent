import json

from conftest import FakeBackend
from doraemon.extract import extract
from evals.labels import Label
from evals.scoring import Score
from test_extract import _item


def _run(bill_email, settings, items, label_json):
    pred = extract(bill_email, FakeBackend(json.dumps({"items": items, "transactions": []})), settings)
    score = Score()
    score.add("001", pred, Label.model_validate(label_json), settings.timezone)
    return score


LABEL = {"reviewed": True, "items": [{"type": "bill", "start_at": "2026-10-16", "amount": "84.20"}]}


def test_correct_prediction(bill_email, settings):
    s = _run(bill_email, settings, [_item()], LABEL)
    assert (s.matched_items, s.wrong_fields, s.pred_items) == (1, 0, 1)


def test_wrong_date_counts_as_field_error(bill_email, settings):
    s = _run(bill_email, settings, [_item(when="October 17, 2026")], LABEL)
    assert (s.matched_items, s.wrong_fields) == (1, 1)


def test_missed_and_extra(bill_email, settings):
    s = _run(bill_email, settings, [_item(type="delivery")], LABEL)
    assert s.matched_items == 0
    assert s.missed_actionable_emails == 0  # something was proposed, just the wrong thing
    assert any("MISSED" in f for f in s.failures)
    assert any("EXTRA" in f for f in s.failures)
