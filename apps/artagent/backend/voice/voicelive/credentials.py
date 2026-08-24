"""Shared credential policy and token-only warmup for Azure VoiceLive."""

from __future__ import annotations

import asyncio
from typing import Any

from azure.core.credentials import AzureKeyCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import DefaultAzureCredential

VOICELIVE_CREDENTIAL_SCOPE = "https://cognitiveservices.azure.com/.default"

_CACHED_DEFAULT_CREDENTIAL: DefaultAzureCredential | None = None
_CREDENTIAL_LOCK = asyncio.Lock()


async def get_voicelive_credential(
    settings: Any,
) -> AzureKeyCredential | AsyncTokenCredential:
    """Return the credential policy shared by warmup and live connections."""

    if settings.azure_voicelive_api_key and not settings.use_default_credential:
        return AzureKeyCredential(settings.azure_voicelive_api_key)

    global _CACHED_DEFAULT_CREDENTIAL
    if _CACHED_DEFAULT_CREDENTIAL is None:
        async with _CREDENTIAL_LOCK:
            if _CACHED_DEFAULT_CREDENTIAL is None:
                _CACHED_DEFAULT_CREDENTIAL = DefaultAzureCredential()
    return _CACHED_DEFAULT_CREDENTIAL


async def warm_voicelive_token(
    settings: Any,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Prefetch one AAD token without opening a VoiceLive model session."""

    if not settings.voicelive_token_warmup_enabled:
        return {
            "status": "disabled",
            "success": True,
            "attempted": False,
            "token_request_count": 0,
        }

    credential = await get_voicelive_credential(settings)
    if isinstance(credential, AzureKeyCredential):
        return {
            "status": "api_key_noop",
            "success": True,
            "attempted": False,
            "token_request_count": 0,
        }

    try:
        await asyncio.wait_for(
            credential.get_token(VOICELIVE_CREDENTIAL_SCOPE),
            timeout=max(0.001, timeout_seconds),
        )
    except TimeoutError:
        return {
            "status": "timeout",
            "success": False,
            "attempted": True,
            "token_request_count": 1,
        }
    except Exception:
        return {
            "status": "credential_error",
            "success": False,
            "attempted": True,
            "token_request_count": 1,
        }

    return {
        "status": "warmed",
        "success": True,
        "attempted": True,
        "token_request_count": 1,
    }


__all__ = [
    "VOICELIVE_CREDENTIAL_SCOPE",
    "get_voicelive_credential",
    "warm_voicelive_token",
]
