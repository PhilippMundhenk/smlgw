from smlgw.auth import generate_secret, hash_password, verify_password


def test_hash_and_verify():
    h = hash_password("s3cret")
    assert h.startswith("pbkdf2$")
    assert verify_password("s3cret", h) is True
    assert verify_password("wrong", h) is False


def test_hash_is_salted_unique():
    assert hash_password("same") != hash_password("same")


def test_verify_handles_missing_or_malformed():
    assert verify_password("x", None) is False
    assert verify_password("x", "") is False
    assert verify_password("x", "garbage") is False


def test_generate_secret_is_random():
    assert generate_secret() != generate_secret()
    assert len(generate_secret()) >= 32
