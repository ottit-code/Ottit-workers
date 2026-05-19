"""Tests for lib/reply_parser.py — quoted-thread stripping."""
from lib import reply_parser


def test_strip_quoted_thread_on_wrote_block():
    text = (
        "Thanks for following up — this looks interesting.\n"
        "What's your availability next week?\n\n"
        "On Mon, Mar 4, 2026 at 10:21 AM Saman <saman@send.ottit.com> wrote:\n"
        "> Hi James, quick thought on month-end close drag...\n"
        "> Best,\n"
        "> Saman\n"
    )
    out = reply_parser.strip_quoted_thread(text)
    assert "this looks interesting" in out
    assert "Hi James" not in out
    assert "wrote:" not in out


def test_strip_quoted_thread_forwarded_block():
    text = (
        "Looping in our COO.\n\n"
        "---------- Forwarded message ----------\n"
        "From: Saman <saman@send.ottit.com>\n"
        "Subject: monthly close\n"
        "Body...\n"
    )
    out = reply_parser.strip_quoted_thread(text)
    assert "Looping in our COO." in out
    assert "Forwarded message" not in out


def test_strip_quoted_thread_empty():
    assert reply_parser.strip_quoted_thread("") == ""
    assert reply_parser.strip_quoted_thread(None) == ""  # type: ignore[arg-type]
