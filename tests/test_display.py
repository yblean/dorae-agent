from datetime import datetime
from zoneinfo import ZoneInfo

from doraemon.display import describe_when
from doraemon.schema import ActionItem

SG = "Asia/Singapore"


def _item(start, tz, all_day=False, departs_from=None):
    return ActionItem(message_id="m", type="flight", title="t", start_at=start, end_at=None,
                      all_day=all_day, timezone=tz, departs_from=departs_from,
                      evidence_snippet="", confidence=0.9)


def test_foreign_time_shows_local_time_too():
    item = _item(datetime(2026, 4, 24, 16, 50, tzinfo=ZoneInfo("Asia/Tokyo")), "Asia/Tokyo", departs_from="Narita International")
    assert describe_when(item, SG) == "Departs Narita International, Fri 24 Apr 2026, 16:50 Japan time (15:50 Singapore time)"


def test_wrong_guess_is_visible():
    # The mistake the model actually made: a Changi departure read as Japan time
    item = _item(datetime(2026, 4, 10, 0, 30, tzinfo=ZoneInfo("Asia/Tokyo")), "Asia/Tokyo", departs_from="Changi Airport")
    assert describe_when(item, SG) == "Departs Changi Airport, Fri 10 Apr 2026, 00:30 Japan time (23:30 Singapore time, 9 Apr)"


def test_home_timezone_and_all_day():
    assert describe_when(_item(datetime(2026, 10, 9, 14, 0, tzinfo=ZoneInfo(SG)), SG), SG) == "Fri 9 Oct 2026, 14:00 Singapore time"
    assert describe_when(_item(datetime(2026, 11, 9, tzinfo=ZoneInfo(SG)), SG, all_day=True), SG) == "Mon 9 Nov 2026 (all day)"
