from datetime import UTC, datetime

import pytest
from src.clinic_recall import voice_cli
from src.clinic_recall.voice_worker import VoiceCadenceResult


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def commit(self) -> None:
        return None


def test_parse_now_requires_timezone() -> None:
    with pytest.raises(ValueError, match="timezone"):
        voice_cli._parse_now("2026-06-28T12:00:00")


def test_parse_now_accepts_zulu_timestamp() -> None:
    assert voice_cli._parse_now("2026-06-28T12:00:00Z") == datetime(
        2026, 6, 28, 12, 0, tzinfo=UTC
    )


def test_voice_cli_runs_planner_without_constructing_provider(monkeypatch, capsys) -> None:
    monkeypatch.setattr(voice_cli, "_bootstrap_runtime_configuration", lambda: None)
    monkeypatch.setattr(
        voice_cli,
        "_runtime_programme_gate",
        lambda _now: (lambda *_args: True),
    )
    monkeypatch.setattr(voice_cli, "get_sessionmaker", lambda: _Session)
    monkeypatch.setattr(
        voice_cli,
        "build_call_initiator",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("planner CLI must not construct a provider")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        voice_cli,
        "run_voice_cadence",
        lambda *_args, **_kwargs: VoiceCadenceResult(),
    )

    assert voice_cli.main(
        ["--clinic-id", "clinic-internal-test", "--now", "2026-06-28T12:00:00Z"]
    ) == 0
    assert '"calls_initiated": 0' in capsys.readouterr().out


def test_voice_cli_without_fresh_pilot_gate_opens_no_database(monkeypatch, capsys) -> None:
    monkeypatch.setattr(voice_cli, "_bootstrap_runtime_configuration", lambda: None)
    monkeypatch.setattr(voice_cli, "_runtime_programme_gate", lambda _now: None)
    monkeypatch.setattr(
        voice_cli,
        "get_sessionmaker",
        lambda: (_ for _ in ()).throw(AssertionError("blocked CLI opened database")),
    )

    assert voice_cli.main(
        ["--clinic-id", "clinic-internal-test", "--now", "2026-06-28T12:00:00Z"]
    ) == 0
    assert '"calls_enqueued": 0' in capsys.readouterr().out