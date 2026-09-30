from framework.folder_lock import (
    hash_recovery_code,
    new_recovery_code,
    new_salt,
    verify_recovery_code,
)


def test_recovery_code_round_trip():
    code = new_recovery_code()
    salt = new_salt()
    stored = hash_recovery_code(code, salt)
    assert len(code) >= 24
    assert verify_recovery_code(code, stored, salt)
    assert not verify_recovery_code(code + "x", stored, salt)


def test_recovery_code_hash_is_not_plaintext():
    code = new_recovery_code()
    assert code not in hash_recovery_code(code, new_salt())


def test_invalid_recovery_code_metadata_fails_closed():
    assert not verify_recovery_code("abc", "", "bad-salt")
