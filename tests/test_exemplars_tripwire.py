"""End-to-end PII tripwire test for the exemplars pipeline.

This is the high-signal regression guard: feed deliberately-leaked PII into
a fixture Drive folder, run it through the *full* loader pipeline
(``loader.get_all_exemplars`` → ``source.collect`` → ``sanitize.sanitize``),
and assert that none of the original sensitive substrings survive in the
returned :class:`Exemplar.text` fields.

Why integrate at the loader level instead of just unit-testing
``sanitize.sanitize`` (which already has its own tripwire)?

Because the failure mode we care most about — a curator-written exemplar
that leaks a real customer's PII into the LLM prompt — depends on
**every step** of the pipeline behaving. A passing ``sanitize`` unit test
does not catch:

* a future refactor that bypasses ``sanitize`` in ``source._load_one``
* a future refactor that caches raw (un-sanitized) text in ``loader``
* a future refactor that returns ``DriveFile.payload`` directly without
  routing through ``extract`` (which is the only place ``sanitize`` runs)

This test pins the contract end-to-end so any regression in any of those
steps fails before reaching production.
"""

from __future__ import annotations

import pytest

from wayonagio_email_agent.exemplars import loader as exemplar_loader
from wayonagio_email_agent.exemplars import source as exemplar_source
from wayonagio_email_agent.exemplars.config import ExemplarConfig
from wayonagio_email_agent.kb.drive import DriveFile


_LEAKY_DOCS = {
    "leak-email": (
        "Refund policy doc.\n\n"
        "If you need to escalate, write directly to maria.rossi@example.com "
        "and CC ops@wayonagio.com on the same thread."
    ),
    "leak-phone": (
        "Pickup logistics.\n\n"
        "Driver Carlos: +51 984 123 456 — call 30 minutes before pickup."
    ),
    "leak-card-iban": (
        "Wire payment to IT60X0542811101000000123456 within 7 days. "
        "If you prefer card, the test number 4242 4242 4242 4242 worked "
        "on the last booking."
    ),
    "leak-booking-url": (
        "Booking confirmed: https://gyg.com/booking/AB12CD34EF56?ref=2024 "
        "(forward to the guest)."
    ),
    "clean": (
        "Standard welcome reply: thank the guest, confirm tour name, "
        "ask for arrival time."
    ),
}


def _drive_file(file_id: str) -> DriveFile:
    return DriveFile(
        id=file_id,
        name=f"{file_id}.gdoc",
        mime_type="application/vnd.google-apps.document",
        path=f"Exemplars / {file_id}.gdoc",
        modified_time="t",
    )


@pytest.fixture(autouse=True)
def _reset_loader_cache():
    exemplar_loader.reset()
    yield
    exemplar_loader.reset()


def test_no_pii_survives_full_pipeline(monkeypatch):
    files = [_drive_file(file_id) for file_id in _LEAKY_DOCS]

    monkeypatch.setattr(
        exemplar_source.kb_drive, "list_folder", lambda *a, **kw: files
    )
    monkeypatch.setattr(
        exemplar_source.kb_drive,
        "read_file",
        lambda df, service=None: _LEAKY_DOCS[df.id].encode("utf-8"),
    )
    monkeypatch.setattr(
        exemplar_source.kb_extract,
        "extract_text",
        lambda df, payload: _LEAKY_DOCS[df.id],
    )
    monkeypatch.setattr(
        exemplar_loader.exemplar_config,
        "load",
        lambda: ExemplarConfig(folder_ids=("fixture",), include_mime_types=()),
    )
    monkeypatch.setattr(
        exemplar_source.kb_drive, "build_drive_service", lambda: object()
    )

    exemplars = exemplar_loader.get_all_exemplars()
    assert len(exemplars) == len(_LEAKY_DOCS)

    forbidden = (
        "maria.rossi@example.com",
        "ops@wayonagio.com",
        "+51 984 123 456",
        "984 123 456",
        "IT60X0542811101000000123456",
        "4242 4242 4242 4242",
        "4242424242424242",
        "AB12CD34EF56",
    )

    full_text = "\n".join(ex.text for ex in exemplars)
    for needle in forbidden:
        assert needle not in full_text, (
            f"PII tripwire BREACH: {needle!r} survived the exemplar pipeline. "
            "Either sanitize.py weakened, or source/loader bypassed it."
        )

    # Sanity: the clean Doc passed through verbatim — sanitization isn't
    # over-redacting safe content.
    clean = next(ex for ex in exemplars if ex.title == "clean.gdoc")
    assert "Standard welcome reply" in clean.text
