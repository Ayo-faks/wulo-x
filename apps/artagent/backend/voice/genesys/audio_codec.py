"""
Audio Codec for Genesys AudioConnector
========================================

Handles audio format conversion between Genesys AudioConnector (µ-law 8kHz)
and Azure VoiceLive API (PCM16 24kHz).

Conversions:
    Inbound (Genesys → VoiceLive):  µ-law 8kHz → PCM16 24kHz (decode + 3x upsample)
    Outbound (VoiceLive → Genesys): PCM16 24kHz → µ-law 8kHz (3x downsample + encode)

Uses numpy for efficient batch processing of audio samples.
"""

from __future__ import annotations

import base64

import numpy as np

# ═══════════════════════════════════════════════════════════════════════════════
# µ-law Decode Table (256 entries: µ-law byte → 16-bit PCM sample)
# ═══════════════════════════════════════════════════════════════════════════════

_ULAW_DECODE_TABLE = np.array(
    [
        -32124, -31100, -30076, -29052, -28028, -27004, -25980, -24956,
        -23932, -22908, -21884, -20860, -19836, -18812, -17788, -16764,
        -15996, -15484, -14972, -14460, -13948, -13436, -12924, -12412,
        -11900, -11388, -10876, -10364, -9852, -9340, -8828, -8316,
        -7932, -7676, -7420, -7164, -6908, -6652, -6396, -6140,
        -5884, -5628, -5372, -5116, -4860, -4604, -4348, -4092,
        -3900, -3772, -3644, -3516, -3388, -3260, -3132, -3004,
        -2876, -2748, -2620, -2492, -2364, -2236, -2108, -1980,
        -1884, -1820, -1756, -1692, -1628, -1564, -1500, -1436,
        -1372, -1308, -1244, -1180, -1116, -1052, -988, -924,
        -876, -844, -812, -780, -748, -716, -684, -652,
        -620, -588, -556, -524, -492, -460, -428, -396,
        -372, -356, -340, -324, -308, -292, -276, -260,
        -244, -228, -212, -196, -180, -164, -148, -132,
        -120, -112, -104, -96, -88, -80, -72, -64,
        -56, -48, -40, -32, -24, -16, -8, 0,
        32124, 31100, 30076, 29052, 28028, 27004, 25980, 24956,
        23932, 22908, 21884, 20860, 19836, 18812, 17788, 16764,
        15996, 15484, 14972, 14460, 13948, 13436, 12924, 12412,
        11900, 11388, 10876, 10364, 9852, 9340, 8828, 8316,
        7932, 7676, 7420, 7164, 6908, 6652, 6396, 6140,
        5884, 5628, 5372, 5116, 4860, 4604, 4348, 4092,
        3900, 3772, 3644, 3516, 3388, 3260, 3132, 3004,
        2876, 2748, 2620, 2492, 2364, 2236, 2108, 1980,
        1884, 1820, 1756, 1692, 1628, 1564, 1500, 1436,
        1372, 1308, 1244, 1180, 1116, 1052, 988, 924,
        876, 844, 812, 780, 748, 716, 684, 652,
        620, 588, 556, 524, 492, 460, 428, 396,
        372, 356, 340, 324, 308, 292, 276, 260,
        244, 228, 212, 196, 180, 164, 148, 132,
        120, 112, 104, 96, 88, 80, 72, 64,
        56, 48, 40, 32, 24, 16, 8, 0,
    ],
    dtype=np.int16,
)

# ═══════════════════════════════════════════════════════════════════════════════
# µ-law Encode Table (exponent lookup for PCM → µ-law conversion)
# ═══════════════════════════════════════════════════════════════════════════════

_ULAW_ENCODE_TABLE = np.array(
    [
        0, 0, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3,
        4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4,
        5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5,
        5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5,
        6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
        6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
        6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
        6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
    ],
    dtype=np.uint8,
)

_ULAW_BIAS = 0x84
_ULAW_CLIP = 32635


def _design_decimator_taps(num_taps: int = 63, cutoff_hz: float = 3600.0, fs_hz: float = 24000.0) -> np.ndarray:
    """Hamming-windowed sinc low-pass for the 24kHz→8kHz decimator.

    63 taps at 24 kHz is ~1.3 ms of group delay (linear phase) — inaudible on
    a phone call — with >50 dB stopband rejection above ~4.4 kHz, so the
    4-12 kHz band that folds onto 0-4 kHz after 3x decimation is suppressed
    instead of aliasing (the old 3-tap boxcar left it at ~-3..-10 dB).
    """
    m = np.arange(num_taps) - (num_taps - 1) / 2.0
    fc = cutoff_hz / fs_hz  # normalized (cycles/sample)
    taps = 2.0 * fc * np.sinc(2.0 * fc * m)
    taps *= np.hamming(num_taps)
    return taps / taps.sum()  # unity DC gain


_DECIMATOR_TAPS = _design_decimator_taps()
_DECIMATOR_HALF = (len(_DECIMATOR_TAPS) - 1) // 2


def ulaw_decode(ulaw_bytes: bytes) -> np.ndarray:
    """Decode µ-law bytes to PCM16 samples at 8kHz.

    Args:
        ulaw_bytes: Raw µ-law encoded audio bytes.

    Returns:
        numpy int16 array of PCM16 samples at 8kHz.
    """
    indices = np.frombuffer(ulaw_bytes, dtype=np.uint8)
    return _ULAW_DECODE_TABLE[indices]


def ulaw_encode(pcm16_samples: np.ndarray) -> bytes:
    """Encode PCM16 samples to µ-law bytes.

    Args:
        pcm16_samples: numpy int16 array of PCM16 audio samples.

    Returns:
        Raw µ-law encoded bytes.
    """
    samples = pcm16_samples.astype(np.int32)
    sign = (samples >> 8) & 0x80
    neg_mask = sign.astype(bool)
    samples = np.where(neg_mask, -samples, samples)
    samples = np.clip(samples, 0, _ULAW_CLIP)
    samples = samples + _ULAW_BIAS
    exponent = _ULAW_ENCODE_TABLE[(samples >> 7) & 0xFF]
    mantissa = (samples >> (exponent.astype(np.int32) + 3)) & 0x0F
    ulaw = ~(sign | (exponent.astype(np.int32) << 4) | mantissa) & 0xFF
    return ulaw.astype(np.uint8).tobytes()


def upsample_3x(pcm_8khz: np.ndarray) -> np.ndarray:
    """Upsample PCM16 from 8kHz to 24kHz using cubic interpolation.

    Args:
        pcm_8khz: PCM16 samples at 8kHz (int16 array).

    Returns:
        PCM16 samples at 24kHz (int16 array, 3x length).
    """
    n = len(pcm_8khz)
    if n == 0:
        return np.array([], dtype=np.int16)

    samples = pcm_8khz.astype(np.float64)
    result = np.empty(n * 3, dtype=np.float64)

    for i in range(n):
        y0 = samples[i - 1] if i > 0 else samples[i]
        y1 = samples[i]
        y2 = samples[i + 1] if i < n - 1 else samples[i]
        y3 = samples[i + 2] if i < n - 2 else y2

        result[i * 3] = y1
        # Cubic interpolation at 1/3 and 2/3 positions
        t1, t2 = 1.0 / 3.0, 2.0 / 3.0
        a0 = y3 - y2 - y0 + y1
        a1 = y0 - y1 - a0
        a2 = y2 - y0
        result[i * 3 + 1] = a0 * t1**3 + a1 * t1**2 + a2 * t1 + y1
        result[i * 3 + 2] = a0 * t2**3 + a1 * t2**2 + a2 * t2 + y1

    return np.clip(np.round(result), -32768, 32767).astype(np.int16)


def downsample_3x(pcm_24khz: np.ndarray) -> np.ndarray:
    """Downsample PCM16 from 24kHz to 8kHz through a windowed-sinc FIR low-pass.

    The previous implementation averaged groups of 3 samples (a 3-tap boxcar),
    whose stopband is only ~-3..-10 dB across the 4-12 kHz alias band. HD
    neural voices carry strong sibilant/breath energy up there, which folded
    back into the audible band as a metallic edge on PSTN calls (live RCA
    2026-07-08). A 63-tap Hamming-windowed sinc with a 3.6 kHz cutoff keeps
    the passband flat and pushes alias-band rejection past -50 dB.

    Stateless per chunk (like the boxcar it replaces): each chunk is
    reflect-padded by half the filter length, so chunk-boundary error is
    limited to the outer ~1.3 ms and there are no clicks from zero padding.

    Args:
        pcm_24khz: PCM16 samples at 24kHz (int16 array).

    Returns:
        PCM16 samples at 8kHz (int16 array, 1/3 length).
    """
    n = len(pcm_24khz)
    out_len = n // 3
    if out_len == 0:
        return np.array([], dtype=np.int16)

    trimmed = pcm_24khz[: out_len * 3].astype(np.float64)
    half = _DECIMATOR_HALF
    if len(trimmed) > 1:
        pad_left = trimmed[1 : half + 1][::-1]
        pad_right = trimmed[-half - 1 : -1][::-1]
        # Short chunks reflect fewer samples than the filter half-length;
        # top up with edge values so 'valid' output length stays exact.
        if len(pad_left) < half:
            pad_left = np.concatenate([np.full(half - len(pad_left), trimmed[0]), pad_left])
        if len(pad_right) < half:
            pad_right = np.concatenate([pad_right, np.full(half - len(pad_right), trimmed[-1])])
    else:
        pad_left = np.full(half, trimmed[0])
        pad_right = np.full(half, trimmed[-1])
    padded = np.concatenate([pad_left, trimmed, pad_right])
    filtered = np.convolve(padded, _DECIMATOR_TAPS, mode="valid")
    decimated = filtered[::3]
    return np.clip(np.round(decimated), -32768, 32767).astype(np.int16)


def ulaw_8khz_to_pcm16_24khz_b64(ulaw_bytes: bytes) -> str:
    """Convert µ-law 8kHz audio to base64-encoded PCM16 24kHz.

    This is the inbound conversion path: Genesys → VoiceLive.

    Args:
        ulaw_bytes: Raw µ-law encoded audio at 8kHz.

    Returns:
        Base64-encoded PCM16 audio at 24kHz for VoiceLive SDK.
    """
    pcm_8khz = ulaw_decode(ulaw_bytes)
    pcm_24khz = upsample_3x(pcm_8khz)
    raw_bytes = pcm_24khz.tobytes()
    return base64.b64encode(raw_bytes).decode("ascii")


def pcm16_24khz_b64_to_ulaw_8khz(pcm16_b64: str) -> bytes:
    """Convert base64-encoded PCM16 24kHz audio to µ-law 8kHz.

    This is the outbound conversion path: VoiceLive → Genesys.

    Args:
        pcm16_b64: Base64-encoded PCM16 audio at 24kHz from VoiceLive.

    Returns:
        Raw µ-law encoded audio at 8kHz for Genesys AudioConnector.
    """
    raw_bytes = base64.b64decode(pcm16_b64)
    return pcm16_24khz_bytes_to_ulaw_8khz(raw_bytes)


def pcm16_24khz_bytes_to_ulaw_8khz(raw_bytes: bytes) -> bytes:
    """Convert raw PCM16 24kHz bytes to µ-law 8kHz.

    Args:
        raw_bytes: Raw PCM16 audio bytes at 24kHz.

    Returns:
        Raw µ-law encoded audio at 8kHz for Genesys AudioConnector.
    """
    # Ensure even byte count for int16 alignment
    if len(raw_bytes) % 2 != 0:
        raw_bytes = raw_bytes[:-1]
    if len(raw_bytes) == 0:
        return b""
    pcm_24khz = np.frombuffer(raw_bytes, dtype=np.int16)
    pcm_8khz = downsample_3x(pcm_24khz)
    if len(pcm_8khz) == 0:
        return b""
    return ulaw_encode(pcm_8khz)


def convert_voicelive_delta_to_ulaw(delta: bytes | str) -> bytes:
    """Convert a VoiceLive audio delta (bytes or base64 str) to µ-law 8kHz.

    The VoiceLive SDK may deliver audio deltas as raw bytes or base64 strings.
    This function handles both formats transparently.

    Args:
        delta: Audio delta from VoiceLive SDK — either raw PCM16 bytes or base64 str.

    Returns:
        Raw µ-law encoded audio at 8kHz for Genesys AudioConnector.
    """
    if isinstance(delta, bytes):
        return pcm16_24khz_bytes_to_ulaw_8khz(delta)
    if isinstance(delta, str):
        return pcm16_24khz_b64_to_ulaw_8khz(delta)
    return b""
