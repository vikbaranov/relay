from app.identity import object_name, pvc_name, session_id

SECRET = b"test-secret"
USER_A = "abc123"
USER_B = "xyz789"


def test_object_name_is_dns_safe():
    name = object_name(SECRET, USER_A)
    assert name.startswith("zc-")
    assert len(name) == 23  # "zc-" + 20 hex chars
    assert name.replace("-", "").isalnum()


def test_object_name_is_deterministic():
    assert object_name(SECRET, USER_A) == object_name(SECRET, USER_A)


def test_different_users_get_different_names():
    assert object_name(SECRET, USER_A) != object_name(SECRET, USER_B)


def test_different_secrets_get_different_names():
    assert object_name(b"secret-1", USER_A) != object_name(b"secret-2", USER_A)


def test_pvc_name_derived_from_object_name():
    name = object_name(SECRET, USER_A)
    assert pvc_name(SECRET, USER_A) == f"{name}-data"


def test_session_id_format():
    value = session_id(f"mattermost_ch1:post1_{USER_A}")
    assert value.startswith("mm-")
    assert len(value) == 27


def test_session_id_uses_generation():
    scope = f"mattermost_ch1:post1_{USER_A}"
    assert session_id(scope, 0) != session_id(scope, 1)
