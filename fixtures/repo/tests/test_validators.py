from validators import validate_email


def test_validate_email_accepts_simple_address():
    assert validate_email("user@example.com") is True


def test_validate_email_rejects_missing_at():
    assert validate_email("userexample.com") is False


def test_validate_email_rejects_empty():
    assert validate_email("") is False
