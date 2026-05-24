"""Tests for argosy.services.pdf_passwords — per-user PDF password store
+ decrypt-and-re-serialize helper used by the chat attachment upload path.

The integration with `save_attachment` is exercised in
`tests/test_turn_attachments.py::test_save_attachment_decrypts_encrypted_pdf_via_password_config`.
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import create_string_object

from argosy.services.pdf_passwords import load_pdf_passwords, try_decrypt_pdf


# ----------------------------------------------------------------------
# Helpers — generate a real encrypted PDF on the fly so tests don't
# depend on user-supplied fixtures.
# ----------------------------------------------------------------------


def _make_encrypted_pdf(password: str) -> bytes:
    """Build a minimal encrypted PDF using pypdf's own encrypt() helper.

    The resulting bytes carry an /Encrypt dict that the same library can
    decrypt back. We can then assert the round-trip works in our helper.
    """
    # Build a one-page PDF in-memory first.
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    # Add some content text so we can confirm it round-trips post-decrypt.
    # pypdf's encryption operates on the entire document including pages.
    writer.add_metadata({"/Title": create_string_object("test-doc")})
    writer.encrypt(user_password="", owner_password=password)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


# ----------------------------------------------------------------------
# load_pdf_passwords
# ----------------------------------------------------------------------


def test_load_returns_empty_when_file_missing(argosy_home_db):
    """No pdf_passwords.json at all → returns [] without raising."""
    assert load_pdf_passwords("nobody") == []


def test_load_returns_passwords_from_dict_shape(tmp_path, monkeypatch, argosy_home_db):
    """Standard documented shape: {"passwords": [...]}."""
    home = Path(argosy_home_db)
    cfg_dir = home / "configs" / "alice"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "pdf_passwords.json").write_text(
        json.dumps({"_note": "test", "passwords": ["abc123", "xyz789"]}),
        encoding="utf-8",
    )
    assert load_pdf_passwords("alice") == ["abc123", "xyz789"]


def test_load_returns_passwords_from_bare_list_shape(argosy_home_db):
    """Tolerant shape: bare list at the top level also works."""
    home = Path(argosy_home_db)
    cfg_dir = home / "configs" / "bob"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "pdf_passwords.json").write_text(
        json.dumps(["one", "two"]),
        encoding="utf-8",
    )
    assert load_pdf_passwords("bob") == ["one", "two"]


def test_load_coerces_ints_to_strings(argosy_home_db):
    """Numeric-string passwords like Israeli IDs may be authored as ints."""
    home = Path(argosy_home_db)
    cfg_dir = home / "configs" / "carol"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "pdf_passwords.json").write_text(
        json.dumps({"passwords": [123456, "abc"]}),
        encoding="utf-8",
    )
    assert load_pdf_passwords("carol") == ["123456", "abc"]


def test_load_malformed_json_returns_empty(argosy_home_db):
    """Bad JSON doesn't crash the upload pipeline; just returns []."""
    home = Path(argosy_home_db)
    cfg_dir = home / "configs" / "dave"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "pdf_passwords.json").write_text("{not valid json", encoding="utf-8")
    assert load_pdf_passwords("dave") == []


def test_load_wrong_shape_returns_empty(argosy_home_db):
    """Top-level neither list nor dict-with-passwords → []."""
    home = Path(argosy_home_db)
    cfg_dir = home / "configs" / "eve"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "pdf_passwords.json").write_text(
        json.dumps({"unrelated": True}),
        encoding="utf-8",
    )
    assert load_pdf_passwords("eve") == []


# ----------------------------------------------------------------------
# try_decrypt_pdf
# ----------------------------------------------------------------------


def test_decrypt_returns_none_when_no_passwords():
    pdf = _make_encrypted_pdf("hunter2")
    assert try_decrypt_pdf(pdf, []) is None


def test_decrypt_returns_unencrypted_bytes_on_match():
    pdf = _make_encrypted_pdf("hunter2")
    out = try_decrypt_pdf(pdf, ["wrong", "hunter2", "alsowrong"])
    assert out is not None
    # Output must no longer be encrypted.
    reader = PdfReader(BytesIO(out))
    assert reader.is_encrypted is False
    # Content survives: still has 1 page.
    assert len(reader.pages) == 1


def test_decrypt_returns_none_when_all_passwords_wrong():
    pdf = _make_encrypted_pdf("hunter2")
    assert try_decrypt_pdf(pdf, ["wrong1", "wrong2"]) is None


def test_decrypt_returns_input_unchanged_for_plain_pdf():
    """Defensive: plain PDF passed through `try_decrypt_pdf` is returned as-is.

    The upload path only calls this when `_is_pdf_encrypted` says True, so
    in production this branch is unreachable. The behavior matters for the
    library API though — return None would force the caller to handle a
    "decrypt of unencrypted PDF" failure mode that doesn't actually exist.
    """
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    out = BytesIO()
    writer.write(out)
    plain = out.getvalue()

    result = try_decrypt_pdf(plain, ["irrelevant"])
    assert result == plain


def test_decrypt_garbage_bytes_returns_none():
    """Non-PDF bytes don't crash the helper."""
    assert try_decrypt_pdf(b"not a pdf at all", ["any"]) is None
