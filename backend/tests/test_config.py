import logging
from legal_workbench.config import load_settings
from legal_workbench.logging_config import RedactSecrets


def test_secrets_do_not_appear_in_settings_or_log(monkeypatch):
    monkeypatch.setenv("LWB_DATABASE_URL", "postgresql://localuser:secret-password@127.0.0.1/test")
    assert "secret-password" not in repr(load_settings())
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "%s %s", ("secret-password", "postgresql://other:secret@host/db"), None)
    assert RedactSecrets().filter(record)
    assert "secret" not in record.getMessage()


def test_url_encoded_password_is_redacted_after_decoding(monkeypatch):
    monkeypatch.setenv("LWB_DATABASE_URL", "postgresql://localuser:secret%20password@127.0.0.1/test")
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "decoded secret password", (), None)
    assert RedactSecrets().filter(record)
    assert "secret password" not in record.getMessage()
