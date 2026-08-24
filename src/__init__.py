"""Shared speech and realtime voice components for Wulo-X."""

__version__ = "0.1.0"
__author__ = "Wulo-X contributors"

# Import main classes for convenience
try:
    from .speech.speech_recognizer import StreamingSpeechRecognizerFromBytes
    from .speech.text_to_speech import SpeechSynthesizer

    __all__ = [
        "SpeechSynthesizer",
        "StreamingSpeechRecognizerFromBytes",
    ]
except ImportError:
    # Handle import errors gracefully during documentation build
    __all__ = []
