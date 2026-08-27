"""Regression: Presenton Cloud OAuth/proxy must stay deleted.

These asserts name the old modules and leftover config keys on purpose.
``LLM == "presenton"`` is input that ``sanitize_provider_settings`` must
strip — not a provider GSlide still offers.
"""

import importlib
import inspect

import pytest

from api.v1.auth.router import API_V1_AUTH_ROUTER
from services.provider_settings import (
    merge_provider_settings,
    sanitize_provider_settings,
)
import utils.get_env as get_env


CLOUD_MODULES = (
    "api.v1.auth.presenton_oauth",
    "services.presenton_cloud",
    "services.presenton_cloud_proxy",
    "services.presenton_cloud_persistence",
    "models.sql.presenton_cloud_provider",
)


@pytest.mark.parametrize("module_name", CLOUD_MODULES)
def test_cloud_modules_are_removed(module_name):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)


def test_oauth_issuer_helpers_are_removed():
    assert not hasattr(get_env, "get_presenton_oauth_issuer")
    assert not hasattr(get_env, "get_presenton_oauth_client_id")
    assert not hasattr(get_env, "DEFAULT_PRESENTON_OAUTH_ISSUER")
    assert not hasattr(get_env, "DEFAULT_PRESENTON_OAUTH_CLIENT_ID")


def test_auth_router_does_not_mount_cloud_oauth():
    paths = [getattr(route, "path", "") for route in API_V1_AUTH_ROUTER.routes]
    assert not any("/presenton" in path for path in paths)


def test_session_auth_middleware_does_not_proxy_cloud():
    from api import middlewares

    source = inspect.getsource(middlewares)
    assert "maybe_proxy_presenton_cloud_request" not in source
    assert "presenton_cloud" not in source


def test_provider_settings_drop_cloud_status_and_llm():
    assert sanitize_provider_settings(
        {
            "LLM": "presenton",
            "OPENAI_API_KEY": "sk",
            "PRESENTON_CONNECTED": True,
            "PRESENTON_EMAIL": "cloud@example.com",
        }
    ) == {"OPENAI_API_KEY": "sk"}


def test_merge_provider_settings_does_not_preserve_cloud_status():
    assert merge_provider_settings(
        {"LLM": "openai", "PRESENTON_CONNECTED": True},
        {"LLM": "google"},
    ) == {"LLM": "google"}
