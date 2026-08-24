"""Spectral regression tests for the 24kHz→8kHz decimator in audio_codec.

Live RCA 2026-07-08: the previous 3-tap boxcar downsampler left the 4-12 kHz
alias band at only ~-3..-10 dB, so HD-voice sibilant/breath energy folded back
into the audible band as a metallic edge on PSTN calls. These tests pin the
windowed-sinc FIR replacement: passband tones survive, alias-band tones die.
"""

from __future__ import annotations

import numpy as np
import pytest
from apps.artagent.backend.voice.genesys.audio_codec import (
    convert_voicelive_delta_to_ulaw,
    downsample_3x,
    ulaw_decode,
)

_FS_IN = 24000
_FS_OUT = 8000


def _tone(freq_hz: float, seconds: float = 0.2, amplitude: int = 12000) -> np.ndarray:
    t = np.arange(int(_FS_IN * seconds)) / _FS_IN
    return (amplitude * np.sin(2 * np.pi * freq_hz * t)).astype(np.int16)


def _band_rms(pcm_8k: np.ndarray, freq_hz: float, width_hz: float = 150.0) -> float:
    """RMS spectral magnitude in a narrow band around freq_hz at 8kHz."""
    spectrum = np.abs(np.fft.rfft(pcm_8k.astype(np.float64)))
    freqs = np.fft.rfftfreq(len(pcm_8k), d=1.0 / _FS_OUT)
    mask = (freqs >= freq_hz - width_hz) & (freqs <= freq_hz + width_hz)
    return float(np.sqrt(np.mean(spectrum[mask] ** 2))) if mask.any() else 0.0


class TestDecimatorSpectralBehavior:
    def test_passband_tone_preserved(self):
        """A 1 kHz voice-band tone must come through at close to full level."""
        tone = _tone(1000)
        out = downsample_3x(tone)
        assert len(out) == len(tone) // 3
        in_rms = np.sqrt(np.mean(tone.astype(np.float64) ** 2))
        out_rms = np.sqrt(np.mean(out.astype(np.float64) ** 2))
        assert out_rms == pytest.approx(in_rms, rel=0.1)

    @pytest.mark.parametrize("alias_freq", [5000.0, 7000.0, 10000.0])
    def test_alias_band_tone_suppressed(self, alias_freq):
        """Tones in the 4-12 kHz alias band must be strongly attenuated, not
        folded back into the audible band (the boxcar regression)."""
        tone = _tone(alias_freq)
        out = downsample_3x(tone)
        # After 3x decimation an unfiltered f aliases to |f - k*8000|.
        folded = abs(alias_freq - round(alias_freq / _FS_OUT) * _FS_OUT)
        alias_rms = _band_rms(out, folded)
        reference = _band_rms(downsample_3x(_tone(1000)), 1000.0)
        # The boxcar left ~-3..-10 dB here; require at least -30 dB.
        assert alias_rms < reference * 0.032, (
            f"{alias_freq} Hz folded to {folded} Hz at {alias_rms:.1f} "
            f"(reference {reference:.1f}) — anti-alias filter regressed"
        )

    def test_dc_gain_unity(self):
        """Silence and DC offsets must not change level (filter normalised)."""
        dc = np.full(2400, 1000, dtype=np.int16)
        out = downsample_3x(dc)
        assert np.abs(out.astype(np.int64) - 1000).max() <= 2

    def test_short_and_empty_chunks(self):
        assert len(downsample_3x(np.array([], dtype=np.int16))) == 0
        assert len(downsample_3x(np.array([100, 200], dtype=np.int16))) == 0
        out = downsample_3x(np.array([100, 200, 300], dtype=np.int16))
        assert len(out) == 1

    def test_full_delta_conversion_roundtrip_shape(self):
        """The VoiceLive-delta entry point still yields 1 ulaw byte per 3 input samples."""
        tone = _tone(800, seconds=0.05)
        ulaw = convert_voicelive_delta_to_ulaw(tone.tobytes())
        assert len(ulaw) == len(tone) // 3
        decoded = ulaw_decode(ulaw)
        # μ-law round trip keeps the tone within companding tolerance.
        in_rms = np.sqrt(np.mean(tone.astype(np.float64) ** 2))
        out_rms = np.sqrt(np.mean(decoded.astype(np.float64) ** 2))
        assert out_rms == pytest.approx(in_rms, rel=0.15)
