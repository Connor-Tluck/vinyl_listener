#!/usr/bin/env python3
"""
Unified runner for Vinyl ID - starts both the web server and audio listener.

Usage:
    python run.py [--device DEVICE_ID] [--port PORT]
"""

import argparse
import asyncio
import signal
import sys
import threading
import time
from datetime import datetime
from typing import Optional

import numpy as np
import sounddevice as sd

from identifier import HybridIdentifier, IdentificationResult
from database import log_listen, init_db
from app import app, update_listener_state


class WebAudioListener:
    """Audio listener that updates the web app state and logs to database."""

    SAMPLE_RATE = 44100
    CHANNELS = 1
    BLOCK_SIZE = 4096

    def __init__(
        self,
        identification_interval: float = 15.0,
        buffer_duration: float = 20.0,
        sample_duration: float = 15.0,
        device: Optional[int] = None,
        acoustid_api_key: Optional[str] = None
    ):
        self.identification_interval = identification_interval
        self.buffer_duration = buffer_duration
        self.sample_duration = sample_duration
        self.device = device

        self.identifier = HybridIdentifier(acoustid_api_key=acoustid_api_key)
        self.running = False
        self.last_result: Optional[IdentificationResult] = None
        self.stream: Optional[sd.InputStream] = None

        # Audio buffer
        self.max_samples = int(buffer_duration * self.SAMPLE_RATE)
        self.buffer = np.zeros(self.max_samples, dtype=np.float32)
        self.buffer_pos = 0
        self.buffer_filled = 0
        self.current_level = 0.0
        self.lock = threading.Lock()

    def _audio_callback(self, indata, frames, time_info, status):
        """Callback for audio stream."""
        if status:
            print(f"Audio status: {status}", file=sys.stderr)

        with self.lock:
            samples = indata.flatten()
            n = len(samples)

            # Add to circular buffer
            if self.buffer_pos + n <= self.max_samples:
                self.buffer[self.buffer_pos:self.buffer_pos + n] = samples
            else:
                # Wrap around
                first_part = self.max_samples - self.buffer_pos
                self.buffer[self.buffer_pos:] = samples[:first_part]
                self.buffer[:n - first_part] = samples[first_part:]

            self.buffer_pos = (self.buffer_pos + n) % self.max_samples
            self.buffer_filled = min(self.buffer_filled + n, self.max_samples)

            # Update audio level
            self.current_level = float(np.sqrt(np.mean(samples ** 2))) * 10

        # Update web state
        update_listener_state(audio_level=self.current_level)

    def _get_audio(self, duration_seconds: float) -> np.ndarray:
        """Get audio from buffer."""
        with self.lock:
            num_samples = min(int(duration_seconds * self.SAMPLE_RATE), self.buffer_filled)
            if num_samples == 0:
                return np.array([], dtype=np.float32)

            # Get most recent samples
            end_pos = self.buffer_pos
            start_pos = (end_pos - num_samples) % self.max_samples

            if start_pos < end_pos:
                return self.buffer[start_pos:end_pos].copy()
            else:
                return np.concatenate([
                    self.buffer[start_pos:],
                    self.buffer[:end_pos]
                ])

    def _format_time(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    async def _identification_loop(self):
        """Main identification loop."""
        while self.running:
            await asyncio.sleep(self.identification_interval)

            if not self.running:
                break

            # Check if we have enough audio
            if self.buffer_filled < self.sample_duration * self.SAMPLE_RATE * 0.8:
                continue

            audio_data = self._get_audio(self.sample_duration)

            # Check for silence
            rms = np.sqrt(np.mean(audio_data ** 2))
            if rms < 0.001:
                update_listener_state(message="Listening (quiet)")
                continue

            # Analyze
            update_listener_state(status="analyzing", message="Analyzing audio...")
            print(f"[{self._format_time()}] Analyzing audio...")

            try:
                result = await self.identifier.identify(audio_data, self.SAMPLE_RATE)

                if result:
                    # Log to database (will return None if duplicate within 5 min)
                    listen_id = log_listen(
                        track=result.track,
                        artist=result.artist,
                        album=result.album,
                        year=result.year,
                        source=result.source,
                        confidence=result.confidence,
                        cover_url=result.cover_url
                    )

                    if listen_id:
                        print(f"[{self._format_time()}] ✓ Identified: {result.artist} - {result.track}")
                        if result.album:
                            print(f"              Album: {result.album} ({result.year or 'Unknown year'})")

                        # Update web state
                        update_listener_state(
                            current_track={
                                "id": listen_id,
                                "track": result.track,
                                "artist": result.artist,
                                "album": result.album,
                                "year": result.year,
                                "source": result.source,
                                "cover_url": result.cover_url
                            }
                        )
                    else:
                        print(f"[{self._format_time()}] Same track playing...")

                else:
                    print(f"[{self._format_time()}] No match found")

                update_listener_state(status="listening", message="Listening")

            except Exception as e:
                print(f"[{self._format_time()}] Error: {e}", file=sys.stderr)
                update_listener_state(status="listening", message="Listening")

    async def run(self):
        """Start the listener."""
        self.running = True

        # Print startup info
        print("\n" + "=" * 50)
        print("  VINYL ID - Audio Listener Started")
        print("=" * 50)

        if self.device is not None:
            device_info = sd.query_devices(self.device)
            print(f"Input device: [{self.device}] {device_info['name']}")
        else:
            default_device = sd.query_devices(sd.default.device[0])
            print(f"Input device: {default_device['name']} (default)")

        print("-" * 50)

        # Start audio stream
        try:
            self.stream = sd.InputStream(
                samplerate=self.SAMPLE_RATE,
                channels=self.CHANNELS,
                blocksize=self.BLOCK_SIZE,
                device=self.device,
                callback=self._audio_callback,
                dtype=np.float32
            )
            self.stream.start()
        except Exception as e:
            print(f"Error starting audio: {e}")
            update_listener_state(status="stopped", message=f"Error: {e}")
            return

        update_listener_state(status="listening", message="Buffering...")

        # Wait for buffer
        await asyncio.sleep(self.sample_duration)
        update_listener_state(message="Listening")

        # Run identification loop
        await self._identification_loop()

    def stop(self):
        """Stop the listener."""
        self.running = False
        if self.stream:
            self.stream.stop()
            self.stream.close()
        update_listener_state(status="stopped", message="Stopped")
        print(f"\n[{self._format_time()}] Stopped.")


def run_flask(port: int):
    """Run Flask in a separate thread."""
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True, use_reloader=False)


def main():
    parser = argparse.ArgumentParser(description="Vinyl ID - Web-based vinyl record identifier")
    parser.add_argument("--device", type=int, default=None, help="Audio input device ID")
    parser.add_argument("--port", type=int, default=5000, help="Web server port (default: 5000)")
    parser.add_argument("--interval", type=float, default=15.0, help="Identification interval in seconds")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit")

    args = parser.parse_args()

    if args.list_devices:
        print("\nAvailable audio input devices:")
        print("-" * 50)
        devices = sd.query_devices()
        for i, device in enumerate(devices):
            if device['max_input_channels'] > 0:
                marker = " (default)" if i == sd.default.device[0] else ""
                print(f"  [{i}] {device['name']}{marker}")
        return

    # Initialize database
    init_db()

    # Create listener
    listener = WebAudioListener(
        identification_interval=args.interval,
        device=args.device
    )

    # Handle Ctrl+C
    def signal_handler(sig, frame):
        listener.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    # Start Flask in background thread
    flask_thread = threading.Thread(target=run_flask, args=(args.port,), daemon=True)
    flask_thread.start()

    print(f"\n🌐 Web UI: http://localhost:{args.port}")
    print("Press Ctrl+C to stop\n")

    # Run async listener
    try:
        asyncio.run(listener.run())
    except KeyboardInterrupt:
        listener.stop()


if __name__ == "__main__":
    main()
