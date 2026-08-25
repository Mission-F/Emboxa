from email.message import EmailMessage

import pytest

from app.mail_parser import parse_and_store
from app.security import decrypt_secret, encrypt_secret, safe_filename, safe_resolve


def test_secret_encryption_and_safe_paths(tmp_path):
    encrypted = encrypt_secret("not-plain-text")
    assert encrypted != "not-plain-text"
    assert decrypt_secret(encrypted) == "not-plain-text"
    assert safe_filename("../../bad\nname.pdf") == "bad_name.pdf"
    assert safe_resolve(tmp_path, "attachments/file") == tmp_path / "attachments" / "file"
    with pytest.raises(ValueError):
        safe_resolve(tmp_path, "../escape")


def test_parse_preserves_eml_inline_and_attachment(tmp_path):
    message = EmailMessage()
    message["From"] = "Alice <alice@example.com>"
    message["To"] = "Bob <bob@example.com>"
    message["Subject"] = "Archive test"
    message["Message-ID"] = "<root@example.com>"
    message.set_content("Plain body searchable")
    message.add_alternative('<p>Hello <img src="cid:pixel"></p>', subtype="html")
    html_part = message.get_payload()[1]
    html_part.add_related(b"PNGDATA", maintype="image", subtype="png", cid="<pixel>", filename="pixel.png")
    message.add_attachment(b"DOCUMENT", maintype="application", subtype="pdf", filename="document.pdf")

    parsed = parse_and_store(message.as_bytes(), tmp_path)
    assert parsed.message_id == "<root@example.com>"
    assert "Plain body searchable" in parsed.text_body
    assert len(parsed.attachments) == 2
    assert (tmp_path / parsed.raw_relpath).is_file()
    assert all((tmp_path / item.relpath).is_file() for item in parsed.attachments)

