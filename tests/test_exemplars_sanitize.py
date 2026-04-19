"""Unit + tripwire tests for exemplars/sanitize.py."""

from __future__ import annotations

import pytest

from wayonagio_email_agent.exemplars.sanitize import sanitize


class TestEmail:
    @pytest.mark.parametrize(
        "raw",
        [
            "Contact us at info@wayonagio.com for details.",
            "Send to John.Doe+booking@example.co.uk please.",
            "Multiple a@b.com and c@d.org in one line.",
        ],
    )
    def test_email_is_redacted(self, raw: str):
        cleaned = sanitize(raw)
        assert "@" not in cleaned or "<EMAIL>" in cleaned
        assert "<EMAIL>" in cleaned

    def test_non_email_with_at_sign_is_left_alone(self):
        # "@" inside Markdown-style mention without a TLD shouldn't trigger.
        text = "See @machupicchu hashtag for photos."
        assert sanitize(text) == text


class TestPhone:
    @pytest.mark.parametrize(
        "raw",
        [
            "Call us at +51 984 123 456 to confirm.",
            "Italy office: +39 02 1234 5678",
            "Mobile: 0049-30-12345678",
            "WhatsApp 984123456 anytime.",
        ],
    )
    def test_phone_is_redacted(self, raw: str):
        assert "<PHONE>" in sanitize(raw)

    def test_short_numeric_runs_left_alone(self):
        """Years and prices have <7 digits; the regex must not redact them."""
        text = "Tour 2026 costs $250 for 4 days."
        cleaned = sanitize(text)
        assert "<PHONE>" not in cleaned
        assert "2026" in cleaned and "250" in cleaned


class TestBookingUrl:
    @pytest.mark.parametrize(
        "raw",
        [
            "https://gyg.com/booking/AB12CD34EF56 confirmed.",
            "Receipt: https://stripe.com/receipts/abcDEF123456",
            "https://wayonagio.com/admin/bookings/2024-AB12CD34EF",
        ],
    )
    def test_booking_url_is_redacted(self, raw: str):
        cleaned = sanitize(raw)
        assert "<BOOKING_URL>" in cleaned
        assert "AB12CD34EF" not in cleaned
        assert "abcDEF123456" not in cleaned

    def test_marketing_url_preserved(self):
        """Plain content URLs (no booking ID) must survive — curators link
        the agency homepage and tour pages all the time."""
        text = "Book at https://wayonagio.com/tours/salkantay today."
        cleaned = sanitize(text)
        assert "https://wayonagio.com/tours/salkantay" in cleaned
        assert "<BOOKING_URL>" not in cleaned


class TestIban:
    @pytest.mark.parametrize(
        "raw",
        [
            "Wire to IT60X0542811101000000123456 within 7 days.",
            "DE89370400440532013000 — our Berlin account.",
            "Spanish IBAN ES9121000418450200051332 included.",
        ],
    )
    def test_iban_is_redacted(self, raw: str):
        cleaned = sanitize(raw)
        assert "<IBAN>" in cleaned

    def test_non_iban_uppercase_string_left_alone(self):
        text = "Tour code MACHU2026FOUR is sold out."
        cleaned = sanitize(text)
        assert cleaned == text


class TestCard:
    @pytest.mark.parametrize(
        # Real Luhn-valid test card numbers (Visa, MC, Amex test ranges).
        "raw",
        [
            "Charged 4242 4242 4242 4242 today.",
            "MC test: 5555-5555-5555-4444 succeeded.",
            "Amex sandbox 378282246310005 worked.",
        ],
    )
    def test_luhn_valid_card_is_redacted(self, raw: str):
        cleaned = sanitize(raw)
        assert "<CARD>" in cleaned

    def test_non_luhn_digit_run_is_not_flagged_as_card(self):
        """A long digit run that fails Luhn must not be marked ``<CARD>``.

        It may still be redacted by the phone pass (a 13+ digit run is
        suspicious enough that defaulting to ``<PHONE>`` is the
        conservative choice for a tripwire), but the ``<CARD>`` marker
        specifically must require Luhn validity so future analytics on
        ``<CARD>`` redactions stay meaningful.
        """
        text_invalid = "Reference 1234567812345671 (not a card)."
        cleaned_invalid = sanitize(text_invalid)
        assert "<CARD>" not in cleaned_invalid

    def test_alphanumeric_reference_with_letters_is_left_alone(self):
        """Real curator references like booking codes are mixed letter+digit
        and don't match the digit-only card / phone patterns."""
        text = "Reference MACHU-2026-A1B2 (sold out)."
        cleaned = sanitize(text)
        assert "MACHU-2026-A1B2" in cleaned
        assert "<CARD>" not in cleaned
        assert "<PHONE>" not in cleaned


class TestEmptyAndWhitespaceInput:
    @pytest.mark.parametrize("raw", ["", "   ", "\n\t\n"])
    def test_returns_unchanged(self, raw: str):
        assert sanitize(raw) == raw


class TestOrderingMatters:
    def test_email_inside_url_handled_by_url_pass_first(self):
        """If a booking URL contains an email param, the URL pass should
        redact the whole URL so the EMAIL pass doesn't spray ``<EMAIL>``
        markers inside an already-redacted URL."""
        text = "https://gyg.com/booking/AB12CD34EF?email=guest@example.com confirmed."
        cleaned = sanitize(text)
        assert "<BOOKING_URL>" in cleaned
        assert "guest@example.com" not in cleaned


class TestTripwire:
    """The single most important test in the module: feed every leak shape
    we know about through ``sanitize`` and assert none of the original
    sensitive substrings survive. If a future edit weakens any individual
    pattern, this test fires immediately.
    """

    def test_no_pii_substrings_survive(self):
        leaky = (
            "Hi! Reply to maria.rossi@example.com or call +39 02 1234 5678. "
            "Booking link: https://gyg.com/booking/AB12CD34EF56?ref=2024 "
            "Wire to IT60X0542811101000000123456. Card on file 4242 4242 4242 4242."
        )
        cleaned = sanitize(leaky)

        forbidden = (
            "maria.rossi@example.com",
            "+39 02 1234 5678",
            "AB12CD34EF56",
            "IT60X0542811101000000123456",
            "4242 4242 4242 4242",
            "4242424242424242",
        )
        for needle in forbidden:
            assert needle not in cleaned, f"{needle!r} survived sanitization"

        for marker in ("<EMAIL>", "<PHONE>", "<BOOKING_URL>", "<IBAN>", "<CARD>"):
            assert marker in cleaned, f"missing {marker} marker"
