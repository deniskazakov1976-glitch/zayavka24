import io
import logging
from pathlib import Path
from typing import BinaryIO, Optional, Union

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


class SpeechToTextService:
    """OpenAI-compatible Speech-to-Text service wrapper (Whisper-1)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "whisper-1",
        client: Optional[AsyncOpenAI] = None,
    ) -> None:
        self.model = model
        self.client = client or AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def transcribe(
        self,
        audio_input: Union[bytes, BinaryIO, Path, str],
        filename: str = "voice.ogg",
    ) -> Optional[str]:
        """Transcribes audio data to text using Whisper."""
        try:
            if isinstance(audio_input, (str, Path)):
                p = Path(audio_input)
                if not p.exists():
                    logger.warning("STT: audio file not found at %s", p)
                    return None
                file_tuple = (p.name, p.read_bytes())
            elif isinstance(audio_input, bytes):
                file_tuple = (filename, audio_input)
            elif hasattr(audio_input, "read"):
                file_tuple = (filename, audio_input.read())
            else:
                logger.warning("STT: invalid audio input type: %s", type(audio_input))
                return None

            response = await self.client.audio.transcriptions.create(
                model=self.model,
                file=file_tuple,
            )
            text = getattr(response, "text", "") or ""
            return text.strip() or None
        except Exception as exc:
            logger.warning("STT transcription failed: %s", exc)
            return None
