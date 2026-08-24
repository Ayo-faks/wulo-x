"""Contract tests pinning the inbound clinic agent's latency-critical YAML knobs.

These values were tuned from the 2026-07-07 latency ledger (see
devops/agentops/voice_latency_eval.py). Changing them shifts caller-perceived
latency and turn-taking behavior — update deliberately, with a fresh ledger.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from apps.artagent.backend.registries.agentstore.loader import discover_agents

_AGENT_YAML = (
    Path(__file__).resolve().parents[1]
    / "apps/artagent/backend/registries/agentstore/inbound_clinic_agent/agent.yaml"
)


def _load() -> dict:
    return yaml.safe_load(_AGENT_YAML.read_text())


class TestInboundClinicAgentTurnDetection:
    def test_semantic_vad_type(self):
        td = _load()["session"]["turn_detection"]
        assert td["type"] == "azure_semantic_vad"

    def test_silence_duration_is_latency_tuned(self):
        """End-of-turn silence window: every reply waits this long after the
        caller stops speaking. 900 ms was the dominant fixed cost per turn;
        550 ms keeps semantic-VAD headroom and matches the production config
        whose handset interruption behavior remains proven."""
        td = _load()["session"]["turn_detection"]
        assert td["silence_duration_ms"] == 550

    def test_prefix_padding_unchanged(self):
        td = _load()["session"]["turn_detection"]
        assert td["prefix_padding_ms"] == 240

    def test_interrupt_flags_preserved(self):
        td = _load()["session"]["turn_detection"]
        assert td["interrupt_response"] is True
        assert td["create_response"] is False
        assert td["auto_truncate"] is True
        assert td["threshold"] == 0.5

    def test_tuned_threshold_reaches_voicelive_sdk(self):
        inbound_agent = discover_agents()["InboundClinicAgent"]
        turn_detection = inbound_agent.build_voicelive_vad()

        assert turn_detection.threshold == 0.5
        assert turn_detection.silence_duration_ms == 550


class TestInboundClinicAgentAudioHygiene:
    """Server-side echo cancellation + noise reduction (2026-07-08 live fix).

    PSTN callers echo the agent's own audio back on the inbound track; without
    AEC the echo reaches VAD/STT and creates phantom turns (greeting cut off,
    \"You're welcome\" replies to silence). Do not remove without a live
    re-test on a real handset.
    """

    def test_echo_cancellation_enabled(self):
        session = _load()["session"]
        assert session["input_audio_echo_cancellation"]["type"] == "server_echo_cancellation"

    def test_noise_reduction_enabled(self):
        session = _load()["session"]
        assert session["input_audio_noise_reduction"]["type"] == "azure_deep_noise_suppression"


class TestInboundClinicAgentVoiceModel:
    def test_voice_identity(self):
        """Dragon HD Omni rendering of the British Sonia persona (2026-07-08).

        Verified synthesizing on the staging VoiceLive resource before pinning.
        HD voices use multiplier rate strings (0.5-1.5): "0.97" ~ the old -3%.
        """
        voice = _load()["voice"]
        assert voice["name"] == "en-GB-Sonia:DragonHDOmniLatestNeural"
        assert voice["type"] == "azure-standard"
        assert voice["rate"] == "0.97"
        assert voice["temperature"] == 0.7

    def test_model_deployment(self):
        """gpt-realtime-1.5 (VoiceLive-managed). Verified 2026-07-08 on staging
        including gpt-4o-transcribe sidecar compatibility."""
        model = _load()["voicelive_model"]
        assert model["deployment_id"] == "gpt-realtime-1.5"
        assert model["temperature"] == 0.2

    def test_transcription_sidecar_unchanged(self):
        """The deterministic safety gate feeds off this sidecar - out of scope
        for the voice/model bump and pinned so a drive-by edit fails loudly."""
        settings = _load()["session"]["input_audio_transcription_settings"]
        assert settings["model"] == "gpt-4o-transcribe"
        assert settings["language"] == "en-GB"


class TestVoiceTemperaturePassthrough:
    """voice.temperature must reach the AzureStandardVoice session payload.

    Before 2026-07-08 the field was silently dropped by VoiceConfig /
    build_voicelive_voice; HD voices use it in place of prosody SSML.
    """

    def test_voice_temperature_reaches_sdk_payload(self):
        from apps.artagent.backend.registries.agentstore.loader import load_agent

        agent = load_agent(_AGENT_YAML, defaults={})
        payload = agent.build_voicelive_voice()
        assert payload is not None
        assert payload.name == "en-GB-Sonia:DragonHDOmniLatestNeural"
        assert payload.rate == "0.97"
        assert payload.temperature == 0.7

    def test_voice_temperature_omitted_when_unset(self):
        from apps.artagent.backend.registries.agentstore.base import VoiceConfig

        cfg = VoiceConfig.from_dict({"name": "en-GB-SoniaNeural"})
        assert cfg.temperature is None
