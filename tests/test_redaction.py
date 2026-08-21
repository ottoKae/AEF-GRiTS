from aef_grits.redaction import redact_text


def test_redacts_tokens_projects_and_authorization_headers():
    text = (
        "Authorization: Bearer abc123 refresh_token=secret "
        "client_secret='hidden' project=gee-personal-example"
    )
    redacted = redact_text(text)
    assert "abc123" not in redacted
    assert "refresh_token=secret" not in redacted
    assert "client_secret='hidden'" not in redacted
    assert "gee-personal-example" not in redacted
