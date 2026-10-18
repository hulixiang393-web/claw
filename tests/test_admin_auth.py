from framework.admin_auth import AdminAuth


def test_admin_auth_configure_and_verify(tmp_path):
    auth = AdminAuth(tmp_path / "admin_auth.json")
    assert not auth.is_configured()
    assert auth.configure("admin-secret")
    assert auth.is_configured()
    assert auth.verify("admin-secret")
    assert not auth.verify("wrong")
    assert not auth.configure("another")


def test_admin_auth_change_requires_old_password(tmp_path):
    auth = AdminAuth(tmp_path / "admin_auth.json")
    auth.configure("admin-secret")
    assert not auth.change("wrong", "new-secret")
    assert auth.change("admin-secret", "new-secret")
    assert auth.verify("new-secret")
    assert not auth.verify("admin-secret")


def test_admin_auth_does_not_store_plaintext(tmp_path):
    path = tmp_path / "admin_auth.json"
    AdminAuth(path).configure("admin-secret")
    assert "admin-secret" not in path.read_text(encoding="utf-8")
