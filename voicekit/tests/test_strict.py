"""Mutation tests for the strict engine: every way of loosening a fake must be caught."""

import pytest

from voicekit.strict import ConformanceError, assert_conforms, assert_object_conforms


async def real_create(*, response=None, event_id=None, additional_instructions=None) -> None: ...


class RealResource:
    async def create(self, *, response=None, event_id=None, additional_instructions=None) -> None: ...
    async def cancel(self, *, response_id=None, event_id=None) -> None: ...


def test_exact_conforming_fake_passes() -> None:
    async def fake(*, response=None, event_id=None, additional_instructions=None) -> None: ...

    assert_conforms(fake, real_create)


def test_kwargs_loosening_is_caught() -> None:
    async def fake(**kwargs) -> None: ...

    with pytest.raises(ConformanceError, match=r"\*\*kwargs"):
        assert_conforms(fake, real_create)


def test_var_positional_loosening_is_caught() -> None:
    async def fake(*args, response=None, event_id=None, additional_instructions=None) -> None: ...

    with pytest.raises(ConformanceError, match=r"\*args"):
        assert_conforms(fake, real_create)


def test_renamed_kwarg_is_caught() -> None:
    async def fake(*, response=None, event_id=None, instructions=None) -> None: ...

    with pytest.raises(ConformanceError) as excinfo:
        assert_conforms(fake, real_create)
    message = str(excinfo.value)
    assert "missing parameter 'additional_instructions'" in message
    assert "'instructions'" in message


def test_dropped_keyword_only_kind_is_caught() -> None:
    async def fake(response=None, event_id=None, additional_instructions=None) -> None: ...

    with pytest.raises(ConformanceError, match="kind mismatch"):
        assert_conforms(fake, real_create)


def test_sync_fake_for_async_real_is_caught() -> None:
    def fake(*, response=None, event_id=None, additional_instructions=None) -> None: ...

    with pytest.raises(ConformanceError, match="async mismatch"):
        assert_conforms(fake, real_create)


def test_required_made_optional_is_caught() -> None:
    async def real(*, item) -> None: ...
    async def fake(*, item=None) -> None: ...

    with pytest.raises(ConformanceError, match="required in the real"):
        assert_conforms(fake, real)


def test_optional_made_required_is_caught() -> None:
    async def fake(*, response, event_id=None, additional_instructions=None) -> None: ...

    with pytest.raises(ConformanceError, match="optional in the real"):
        assert_conforms(fake, real_create)


def test_object_conformance_flags_missing_method() -> None:
    class Fake:
        async def create(self, *, response=None, event_id=None, additional_instructions=None) -> None: ...

    with pytest.raises(ConformanceError, match="missing method 'cancel'"):
        assert_object_conforms(Fake(), RealResource)


def test_object_conformance_accepts_conforming_fake() -> None:
    class Fake:
        async def create(self, *, response=None, event_id=None, additional_instructions=None) -> None: ...
        async def cancel(self, *, response_id=None, event_id=None) -> None: ...

    assert_object_conforms(Fake(), RealResource)
