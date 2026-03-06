"""
Music identification module using ShazamIO and AcoustID.
"""

import asyncio
import io
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy.io import wavfile


@dataclass
class IdentificationResult:
    """Result from music identification."""
    track: str
    artist: str
    album: Optional[str] = None
    year: Optional[str] = None
    source: str = "unknown"
    confidence: Optional[float] = None
    cover_url: Optional[str] = None
    track_number: Optional[int] = None
    genre: Optional[str] = None


class BaseIdentifier(ABC):
    """Base class for music identifiers."""

    @abstractmethod
    async def identify(self, audio_data: np.ndarray, sample_rate: int) -> Optional[IdentificationResult]:
        """Identify music from audio data."""
        pass


class ShazamIdentifier(BaseIdentifier):
    """Identifies music using ShazamIO (reverse-engineered Shazam API)."""

    def __init__(self, max_retries: int = 5):
        self.max_retries = max_retries
        self._shazam = None

    def _get_shazam(self):
        """Lazy load Shazam instance."""
        if self._shazam is None:
            from shazamio import Shazam
            self._shazam = Shazam()
        return self._shazam

    def _audio_to_wav_bytes(self, audio_data: np.ndarray, sample_rate: int) -> bytes:
        """Convert numpy audio array to WAV bytes."""
        # Ensure audio is in correct format (16-bit PCM)
        if audio_data.dtype == np.float32 or audio_data.dtype == np.float64:
            # Convert float [-1, 1] to int16
            audio_int16 = (audio_data * 32767).astype(np.int16)
        else:
            audio_int16 = audio_data.astype(np.int16)

        # Write to bytes buffer
        buffer = io.BytesIO()
        wavfile.write(buffer, sample_rate, audio_int16)
        return buffer.getvalue()

    async def identify(self, audio_data: np.ndarray, sample_rate: int) -> Optional[IdentificationResult]:
        """Identify music using Shazam."""
        shazam = self._get_shazam()
        wav_bytes = self._audio_to_wav_bytes(audio_data, sample_rate)

        # Retry with exponential backoff for rate limiting
        for attempt in range(self.max_retries):
            try:
                result = await shazam.recognize(wav_bytes)

                if result and 'track' in result:
                    track_info = result['track']
                    # Extract album, year, track number, and genre from metadata
                    album = None
                    year = None
                    track_number = None
                    genre = None
                    sections = track_info.get('sections', [])
                    for section in sections:
                        metadata = section.get('metadata', [])
                        for item in metadata:
                            title = item.get('title', '').lower()
                            text = item.get('text', '')
                            if title == 'album':
                                album = text
                            elif title == 'released':
                                year = text
                            elif title == 'track':
                                try:
                                    track_number = int(text)
                                except (ValueError, TypeError):
                                    pass
                    # Get genre from genres array
                    genres = track_info.get('genres', {})
                    if genres and 'primary' in genres:
                        genre = genres['primary']
                    return IdentificationResult(
                        track=track_info.get('title', 'Unknown'),
                        artist=track_info.get('subtitle', 'Unknown'),
                        album=album,
                        year=year,
                        source='Shazam',
                        cover_url=track_info.get('images', {}).get('coverart'),
                        track_number=track_number,
                        genre=genre
                    )
                return None

            except Exception as e:
                error_str = str(e).lower()
                if '429' in error_str or 'rate' in error_str:
                    # Rate limited - exponential backoff
                    wait_time = (2 ** attempt) + (0.1 * attempt)
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    # Other error - log and return None
                    print(f"Shazam error: {e}")
                    return None

        return None


class AcoustIDIdentifier(BaseIdentifier):
    """Identifies music using AcoustID/Chromaprint (MusicBrainz)."""

    def __init__(self, api_key: Optional[str] = None):
        # AcoustID allows anonymous queries for basic lookups
        self.api_key = api_key or "vNPPnj9tAp"  # Demo key - users should get their own

    async def identify(self, audio_data: np.ndarray, sample_rate: int) -> Optional[IdentificationResult]:
        """Identify music using AcoustID."""
        try:
            import acoustid
        except ImportError:
            print("pyacoustid not installed or chromaprint not available")
            return None

        # Run in executor since acoustid is synchronous
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._identify_sync, audio_data, sample_rate)

    def _identify_sync(self, audio_data: np.ndarray, sample_rate: int) -> Optional[IdentificationResult]:
        """Synchronous identification using acoustid."""
        import acoustid
        import tempfile
        import os

        # Write audio to temp file for chromaprint
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            temp_path = f.name
            # Convert to int16 if needed
            if audio_data.dtype == np.float32 or audio_data.dtype == np.float64:
                audio_int16 = (audio_data * 32767).astype(np.int16)
            else:
                audio_int16 = audio_data.astype(np.int16)
            wavfile.write(f, sample_rate, audio_int16)

        try:
            # Get fingerprint and lookup
            results = acoustid.match(self.api_key, temp_path)

            for score, recording_id, title, artist in results:
                if score > 0.5:  # Minimum confidence threshold
                    return IdentificationResult(
                        track=title or "Unknown",
                        artist=artist or "Unknown",
                        source='AcoustID',
                        confidence=score
                    )
            return None

        except acoustid.NoBackendError:
            print("Chromaprint not installed. Install with: brew install chromaprint")
            return None
        except acoustid.FingerprintGenerationError:
            print("Could not generate fingerprint from audio")
            return None
        except acoustid.WebServiceError as e:
            print(f"AcoustID service error: {e}")
            return None
        finally:
            os.unlink(temp_path)


class HybridIdentifier(BaseIdentifier):
    """Tries multiple identification services with fallback."""

    def __init__(self, acoustid_api_key: Optional[str] = None):
        self.shazam = ShazamIdentifier()
        self.acoustid = AcoustIDIdentifier(api_key=acoustid_api_key)

    async def identify(self, audio_data: np.ndarray, sample_rate: int) -> Optional[IdentificationResult]:
        """Try Shazam first, fall back to AcoustID."""
        # Try Shazam first (larger database, usually faster)
        result = await self.shazam.identify(audio_data, sample_rate)
        if result:
            return result

        # Fall back to AcoustID
        result = await self.acoustid.identify(audio_data, sample_rate)
        if result:
            return result

        return None
