from app.security.password import hash_password, verify_password


def test_hash_and_verify_round_trip_succeeds_for_matching_password():
    hashed = hash_password("correct horse battery staple")

    assert verify_password("correct horse battery staple", hashed) is True


def test_verify_rejects_wrong_password():
    hashed = hash_password("correct horse battery staple")

    assert verify_password("wrong password", hashed) is False


def test_hash_output_is_not_plaintext_and_is_salted_per_call():
    first = hash_password("same-password")
    second = hash_password("same-password")

    assert first != "same-password"
    assert first != second
    assert verify_password("same-password", first) is True
    assert verify_password("same-password", second) is True


def test_verify_rejects_malformed_hash_instead_of_raising():
    assert verify_password("anything", "not-a-real-argon2-hash") is False
