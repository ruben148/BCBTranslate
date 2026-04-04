from __future__ import annotations

import html


class SSMLBuilder:
    """Construct SSML strings for Azure TTS with prosody control."""

    @staticmethod
    def build(
        text: str,
        voice: str,
        rate: float = 1.0,
        pitch: str = "+0%",
        volume: int = 100,
    ) -> str:
        escaped = html.escape(text, quote=True)
        rate_str = f"{rate:.2f}"
        lang = SSMLBuilder._voice_to_lang(voice)
        return (
            f'<speak version="1.0" '
            f'xmlns="http://www.w3.org/2001/10/synthesis" '
            f'xml:lang="{lang}">'
            f'<voice name="{voice}">'
            f'<prosody rate="{rate_str}" pitch="{pitch}" volume="{volume}">'
            f"{escaped}"
            f"</prosody></voice></speak>"
        )

    @staticmethod
    def _voice_to_lang(voice: str) -> str:
        """Extract locale from voice name (e.g. 'en-US-JennyNeural' -> 'en-US')."""
        parts = voice.split("-")
        if len(parts) >= 2:
            return f"{parts[0]}-{parts[1]}"
        return "en-US"
