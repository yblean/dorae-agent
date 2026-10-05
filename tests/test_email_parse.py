from datetime import datetime, timezone

from doraemon.email_parse import forwarded_original_recipients, parse_eml, strip_quoted_reply


def test_parses_headers_and_html_body(bill_email):
    assert bill_email.message_id == "<bill-001@acmepower.example>"
    assert bill_email.subject == "Your October electricity bill is ready"
    assert bill_email.sent_at == datetime(2026, 10, 4, 9, 15, tzinfo=timezone.utc)
    assert "$84.20" in bill_email.body
    assert "Friday, October 16, 2026" in bill_email.body


def test_strips_zero_width_characters():
    raw = "Subject: x\nContent-Type: text/plain; charset=utf-8\n\nBooking No. 1‌578‌950".encode()
    assert parse_eml(raw).body == "Booking No. 1578950"


def test_strips_urls_and_image_markers():
    raw = (b"Subject: x\nContent-Type: text/plain\n\n"
           b"Manage booking <https://trip.example/a?b=c> or visit https://x.example/y [cid:abc-123] now")
    assert parse_eml(raw).body == "Manage booking or visit now"


def test_stub_plain_part_falls_back_to_html():
    raw = (b'Subject: x\nMIME-Version: 1.0\nContent-Type: multipart/alternative; boundary="b"\n\n'
           b"--b\nContent-Type: text/plain\n\nundefined\n"
           b"--b\nContent-Type: text/html\n\n<p>Jay Chou tickets on sale 12 Nov</p>\n--b--\n")
    assert parse_eml(raw).body == "Jay Chou tickets on sale 12 Nov"


def test_reply_drops_quoted_original():
    body = ("Please bring your laptop tomorrow.\n\nBest Regards\n"
            "________________________________\nFrom: Lucy\nSent: Friday\n\nBTC Day is on 7 August 2026.")
    assert strip_quoted_reply("Re: [REMINDER] BTC Day", body) == "Please bring your laptop tomorrow.\n\nBest Regards"
    gmail = "Sounds good!\n\nOn Mon, 5 Oct 2026 at 10:00, Bob <b@x.com> wrote:\n> Dinner at 7?"
    assert strip_quoted_reply("RE: dinner", gmail).strip() == "Sounds good!"


def test_forwarded_original_recipient_is_innermost():
    body = ("FYI\n---------- Forwarded message ---------\nFrom: Matthew <m@x.com>\n"
            "To: Yi Bin <yibin@x.com>, z@x.com\n\n---------- Forwarded message ---------\n"
            "From: flychinaeastern <a@ceair.com>\nDate: Fri\nTo: <m@x.com>\n\nTicket issued")
    assert forwarded_original_recipients("Fwd: Fwd: Ticket issued", body) == ["m@x.com"]
    assert forwarded_original_recipients("Ticket issued", body) is None  # not a forward


def test_forward_keeps_quoted_content():
    body = "---------- Forwarded message ---------\nFrom: Trip.com\nCheck-in: Nov 9, 2026"
    assert strip_quoted_reply("Fwd: Booking confirmation", body) == body


def test_drops_script_and_style(bill_email):
    assert "track()" not in bill_email.body
    assert "color: red" not in bill_email.body
