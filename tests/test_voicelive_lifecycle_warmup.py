import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

from apps.artagent.backend.lifecycle.manager import LifecycleManager
from apps.artagent.backend.lifecycle.steps import register_warmup_step
from apps.artagent.backend.voice.voicelive import credentials as credentials_module
from apps.artagent.backend.voice.voicelive import settings as settings_module
from fastapi import FastAPI

aoai_client_module = importlib.import_module("src.aoai.client")


async def test_lifecycle_reports_nonblocking_voicelive_token_result(
    monkeypatch,
) -> None:
    token_result = {
        "status": "credential_error",
        "success": False,
        "attempted": True,
        "token_request_count": 1,
    }
    warm_token = AsyncMock(return_value=token_result)
    monkeypatch.setattr(credentials_module, "warm_voicelive_token", warm_token)
    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: SimpleNamespace(voicelive_token_warmup_timeout_seconds=0.25),
    )
    monkeypatch.setattr(
        aoai_client_module,
        "warm_openai_connection",
        AsyncMock(return_value=True),
        raising=False,
    )
    app = FastAPI()
    manager = LifecycleManager()
    register_warmup_step(manager, app)

    await manager.deferred_steps[0].startup()

    assert app.state.warmup_completed is True
    assert app.state.warmup_results["voicelive_token"] == token_result
    assert app.state.warmup_results["openai"] is True
    warm_token.assert_awaited_once()
    assert warm_token.await_args.kwargs == {"timeout_seconds": 0.25}
