import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import azure.ai.voicelive.aio as voicelive_aio
from apps.artagent.backend.voice.voicelive import credentials as credentials_module
from apps.artagent.backend.voice.voicelive.credentials import (
    VOICELIVE_CREDENTIAL_SCOPE,
    warm_voicelive_token,
)
from apps.artagent.backend.voice.voicelive.settings import VoiceLiveSettings
from azure.core.credentials import AzureKeyCredential


def _settings(*, enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        azure_voicelive_api_key=None,
        use_default_credential=True,
        voicelive_token_warmup_enabled=enabled,
    )


def test_voicelive_token_warmup_setting_defaults_off() -> None:
    settings = VoiceLiveSettings(
        azure_voicelive_endpoint="https://voice.example.invalid",
        _env_file=None,
    )

    assert settings.voicelive_token_warmup_enabled is False
    assert settings.voicelive_token_warmup_timeout_seconds == 5.0


async def test_voicelive_token_warmup_is_default_off(monkeypatch) -> None:
    get_credential = AsyncMock()
    monkeypatch.setattr(
        credentials_module,
        "get_voicelive_credential",
        get_credential,
    )

    result = await warm_voicelive_token(_settings(enabled=False), timeout_seconds=1.0)

    assert result == {
        "status": "disabled",
        "success": True,
        "attempted": False,
        "token_request_count": 0,
    }
    get_credential.assert_not_awaited()


async def test_voicelive_token_warmup_uses_cognitive_services_scope(
    monkeypatch,
) -> None:
    credential = SimpleNamespace(get_token=AsyncMock(return_value=object()))
    provider_connect = AsyncMock()
    monkeypatch.setattr(
        credentials_module,
        "get_voicelive_credential",
        AsyncMock(return_value=credential),
    )
    monkeypatch.setattr(voicelive_aio, "connect", provider_connect)

    result = await warm_voicelive_token(_settings(enabled=True), timeout_seconds=1.0)

    assert result["status"] == "warmed"
    assert result["token_request_count"] == 1
    credential.get_token.assert_awaited_once_with(VOICELIVE_CREDENTIAL_SCOPE)
    provider_connect.assert_not_called()


async def test_voicelive_token_warmup_is_api_key_noop(monkeypatch) -> None:
    monkeypatch.setattr(
        credentials_module,
        "get_voicelive_credential",
        AsyncMock(return_value=AzureKeyCredential("not-logged")),
    )

    result = await warm_voicelive_token(_settings(enabled=True), timeout_seconds=1.0)

    assert result == {
        "status": "api_key_noop",
        "success": True,
        "attempted": False,
        "token_request_count": 0,
    }


async def test_voicelive_token_warmup_timeout_is_non_blocking(monkeypatch) -> None:
    async def never_returns(_scope: str) -> None:
        await asyncio.Event().wait()

    credential = SimpleNamespace(get_token=never_returns)
    monkeypatch.setattr(
        credentials_module,
        "get_voicelive_credential",
        AsyncMock(return_value=credential),
    )

    result = await warm_voicelive_token(
        _settings(enabled=True),
        timeout_seconds=0.001,
    )

    assert result == {
        "status": "timeout",
        "success": False,
        "attempted": True,
        "token_request_count": 1,
    }
