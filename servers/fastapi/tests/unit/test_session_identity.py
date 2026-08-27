from api.v1.auth.config import (
    LEGACY_SESSION_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    read_session_token,
)


def test_session_cookie_names():
    assert SESSION_COOKIE_NAME == "gslide_session"
    assert LEGACY_SESSION_COOKIE_NAME == "presenton_session"


def test_read_session_token_prefers_gslide_cookie():
    assert (
        read_session_token(
            {
                "gslide_session": "new-jwt",
                "presenton_session": "old-jwt",
            }
        )
        == "new-jwt"
    )


def test_read_session_token_falls_back_to_legacy():
    assert read_session_token({"presenton_session": "old-jwt"}) == "old-jwt"


def test_read_session_token_ignores_empty_new_cookie():
    assert (
        read_session_token({"gslide_session": "", "presenton_session": "old-jwt"})
        == "old-jwt"
    )


import uuid

from models.sql.access_token import AccessToken
from api.v1.auth.config import (
    API_KEY_PREFIX,
    LEGACY_API_KEY_PREFIX,
    is_accepted_api_key,
)


def test_api_key_prefixes():
    assert API_KEY_PREFIX == "sk-gslide-"
    assert LEGACY_API_KEY_PREFIX == "sk-presenton-"
    assert is_accepted_api_key("sk-gslide-abc")
    assert is_accepted_api_key("sk-presenton-abc")
    assert not is_accepted_api_key("sk-other-abc")


def test_new_access_token_uses_gslide_prefix():
    token = AccessToken(user_id=uuid.uuid4())
    assert token.token.startswith("sk-gslide-")


def test_reset_password_secret_is_gslide(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'fastapi.db').as_posix()}")

    from api.v1.auth.users import UserManager

    assert UserManager.reset_password_token_secret == "gslide-admin-managed-passwords"


def test_owner_context_var_names_are_gslide():
    from api.v1.auth.context import _CURRENT_OWNER_ID

    assert _CURRENT_OWNER_ID.name == "gslide_current_owner_id"