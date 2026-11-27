import pytest

from goat.gift_preview import GiftPreviews, parse_image
from goat.terminal import demo


def test_telegram_preview_requires_matching_number_and_official_cdn():
    html = '''<meta property="og:title" content="Snoop Dogg #223555">
    <meta property="og:site_name" content="Telegram">
    <meta property="og:image" content="https://cdn5.telesco.pe/file/abc-123.jpg">'''
    assert parse_image(html, "snoopdogg-223555") == "https://cdn5.telesco.pe/file/abc-123.jpg"
    assert parse_image(html, "snoopdogg-223556") is None
    assert parse_image(html.replace("cdn5.telesco.pe", "cdn5.telesco.pe.example.org"), "snoopdogg-223555") is None


async def test_invalid_slug_is_not_requested():
    assert await GiftPreviews().resolve("https://example.org") is None


def test_demo_uses_telegram_number_and_marks_training_prices():
    details = demo()[0]["details"]
    assert details["telegram_slug"] == "snoopdogg-223555"
    assert details["number"] == 223555
    assert details["backdrop"] == "Deep Cyan"
    assert "учебные" in details["risk"]
