#!/usr/bin/env python3
"""
Vinyl Record Identifier - Continuous audio listener that identifies playing songs.

Usage:
    python vinyl_listener.py [--interval SECONDS] [--duration SECONDS] [--device DEVICE_ID]

Examples:
    python vinyl_listener.py                    # Default settings
    python vinyl_listener.py --interval 20     # Check every 20 seconds
    python vinyl_listener.py --device 1        # Use specific audio device
"""

import argparse
import asyncio
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

import numpy as np
import sounddevice as sd

from identifier import HybridIdentifier, IdentificationResult


class SpinnerAnimation:
    """Animated spinner for visual feedback."""

    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    AUDIO_BARS = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]

    def __init__(self):
        self.frame_index = 0
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.message = "Listening"
        self.audio_level = 0.0
        self.lock = threading.Lock()

    def _get_level_bar(self) -> str:
        """Get audio level visualization."""
        # Map audio level (0-1) to bar characters
        bars = ""
        for i in range(8):
            threshold = i / 8
            if self.audio_level > threshold:
                bar_idx = min(int((self.audio_level - threshold) * 64), 7)
                bars += self.AUDIO_BARS[bar_idx]
            else:
                bars += "▁"
        return bars

    def _animate(self):
        """Animation loop running in background thread."""
        while self.running:
            with self.lock:
                spinner = self.FRAMES[self.frame_index]
                level_bar = self._get_level_bar()
                time_str = datetime.now().strftime("%H:%M:%S")
                print(f"\r[{time_str}] {spinner} {self.message}  {level_bar}  ", end="", flush=True)
                self.frame_index = (self.frame_index + 1) % len(self.FRAMES)
            time.sleep(0.1)

    def start(self, message: str = "Listening"):
        """Start the spinner animation."""
        self.message = message
        self.running = True
        self.thread = threading.Thread(target=self._animate, daemon=True)
        self.thread.start()

    def update_level(self, level: float):
        """Update the audio level (0.0 to 1.0)."""
        with self.lock:
            self.audio_level = min(max(level, 0.0), 1.0)

    def update_message(self, message: str):
        """Update the spinner message."""
        with self.lock:
            self.message = message

    def stop(self):
        """Stop the spinner animation."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=0.5)
        print("\r" + " " * 60 + "\r", end="", flush=True)  # Clear line


class AudioBuffer:
    """Thread-safe ring buffer for audio samples."""

    def __init__(self, max_duration_seconds: float, sample_rate: int):
        self.sample_rate = sample_rate
        self.max_samples = int(max_duration_seconds * sample_rate)
        self.buffer = deque(maxlen=self.max_samples)
        self.lock = threading.Lock()
        self.current_level = 0.0

    def add_samples(self, samples: np.ndarray):
        """Add audio samples to the buffer."""
        with self.lock:
            flat_samples = samples.flatten()
            for sample in flat_samples:
                self.buffer.append(sample)
            # Update current audio level (RMS)
            self.current_level = float(np.sqrt(np.mean(flat_samples ** 2)))

    def get_level(self) -> float:
        """Get current audio level."""
        with self.lock:
            return self.current_level

    def get_audio(self, duration_seconds: Optional[float] = None) -> np.ndarray:
        """Get audio from buffer as numpy array."""
        with self.lock:
            if duration_seconds:
                num_samples = min(int(duration_seconds * self.sample_rate), len(self.buffer))
                # Get most recent samples
                samples = list(self.buffer)[-num_samples:]
            else:
                samples = list(self.buffer)
            return np.array(samples, dtype=np.float32)

    def duration_seconds(self) -> float:
        """Current buffer duration in seconds."""
        return len(self.buffer) / self.sample_rate


class VinylListener:
    """Continuous audio listener for vinyl record identification."""

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

        self.audio_buffer = AudioBuffer(buffer_duration, self.SAMPLE_RATE)
        self.identifier = HybridIdentifier(acoustid_api_key=acoustid_api_key)

        self.running = False
        self.last_result: Optional[IdentificationResult] = None
        self.stream: Optional[sd.InputStream] = None
        self.spinner = SpinnerAnimation()

    def _audio_callback(self, indata, frames, time_info, status):
        """Callback for audio stream - adds samples to buffer."""
        if status:
            print(f"Audio status: {status}", file=sys.stderr)
        self.audio_buffer.add_samples(indata)
        # Update spinner with current audio level (scaled for visibility)
        level = self.audio_buffer.get_level() * 10  # Scale up for better visualization
        self.spinner.update_level(level)

    def _format_time(self) -> str:
        """Format current time for display."""
        return datetime.now().strftime("%H:%M:%S")

    def _print_result(self, result: IdentificationResult):
        """Print identification result to console."""
        self.spinner.stop()
        print(f"\n[{self._format_time()}] \033[92m✓ Identified via {result.source}:\033[0m")
        print(f"           Track:  \"{result.track}\"")
        print(f"           Artist: {result.artist}")
        if result.album:
            print(f"           Album:  {result.album}")
        if result.confidence:
            print(f"           Confidence: {result.confidence:.1%}")
        print("─" * 50)
        self.spinner.start("Listening")

    def _print_no_match(self):
        """Print when no match is found."""
        self.spinner.stop()
        print(f"[{self._format_time()}] No match found, continuing to listen...")
        self.spinner.start("Listening")

    async def _identification_loop(self):
        """Main identification loop - runs periodically."""
        while self.running:
            # Wait for enough audio
            await asyncio.sleep(self.identification_interval)

            if not self.running:
                break

            # Get audio sample
            if self.audio_buffer.duration_seconds() < self.sample_duration * 0.8:
                continue

            audio_data = self.audio_buffer.get_audio(self.sample_duration)

            # Check if there's actual audio (not silence)
            rms = np.sqrt(np.mean(audio_data ** 2))
            if rms < 0.001:  # Very quiet - likely no music playing
                self.spinner.update_message("Listening (quiet)")
                continue

            # Try to identify
            self.spinner.update_message("Analyzing audio...")

            try:
                result = await self.identifier.identify(audio_data, self.SAMPLE_RATE)

                if result:
                    # Avoid printing duplicate results
                    if (self.last_result is None or
                        result.track != self.last_result.track or
                        result.artist != self.last_result.artist):
                        self._print_result(result)
                        self.last_result = result
                    else:
                        self.spinner.update_message("Listening (same track)")
                else:
                    self._print_no_match()

                self.spinner.update_message("Listening")

            except Exception as e:
                print(f"\r[{self._format_time()}] Error during identification: {e}", file=sys.stderr)

    def _list_devices(self):
        """List available audio input devices."""
        print("\nAvailable audio input devices:")
        print("-" * 50)
        devices = sd.query_devices()
        for i, device in enumerate(devices):
            if device['max_input_channels'] > 0:
                marker = " (default)" if i == sd.default.device[0] else ""
                print(f"  [{i}] {device['name']}{marker}")
        print()

    async def run(self):
        """Main run loop."""
        self.running = True

        # Print header
        print("\n" + "=" * 50)
        print("  VINYL RECORD IDENTIFIER")
        print("=" * 50)
        print(f"Sample rate: {self.SAMPLE_RATE} Hz")
        print(f"Identification interval: {self.identification_interval}s")
        print(f"Sample duration: {self.sample_duration}s")

        if self.device is not None:
            device_info = sd.query_devices(self.device)
            print(f"Input device: [{self.device}] {device_info['name']}")
        else:
            default_device = sd.query_devices(sd.default.device[0])
            print(f"Input device: {default_device['name']} (default)")

        print("-" * 50)
        print("Press Ctrl+C to stop\n")

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
            print(f"Error starting audio stream: {e}")
            print("\nTry listing devices with --list-devices and selecting one with --device")
            return

        # Wait for buffer to fill initially
        self.spinner.start("Buffering audio...")
        await asyncio.sleep(self.sample_duration)
        self.spinner.update_message("Listening")

        # Run identification loop
        await self._identification_loop()

    def stop(self):
        """Stop the listener."""
        self.running = False
        self.spinner.stop()
        if self.stream:
            self.stream.stop()
            self.stream.close()
        print(f"\n[{self._format_time()}] Stopped.")


def main():
    parser = argparse.ArgumentParser(
        description="Vinyl Record Identifier - Continuously listens and identifies playing music",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          # Start with default settings
  %(prog)s --interval 20            # Check every 20 seconds
  %(prog)s --duration 20            # Use 20 seconds of audio for identification
  %(prog)s --list-devices           # Show available audio devices
  %(prog)s --device 2               # Use audio device with ID 2
        """
    )
    parser.add_argument(
        "--interval", "-i",
        type=float,
        default=15.0,
        help="Identification interval in seconds (default: 15)"
    )
    parser.add_argument(
        "--duration", "-d",
        type=float,
        default=15.0,
        help="Audio sample duration for identification in seconds (default: 15)"
    )
    parser.add_argument(
        "--device",
        type=int,
        default=None,
        help="Audio input device ID (use --list-devices to see options)"
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List available audio input devices and exit"
    )
    parser.add_argument(
        "--acoustid-key",
        type=str,
        default=None,
        help="AcoustID API key (optional, uses demo key if not provided)"
    )

    args = parser.parse_args()

    if args.list_devices:
        listener = VinylListener()
        listener._list_devices()
        return

    listener = VinylListener(
        identification_interval=args.interval,
        sample_duration=args.duration,
        device=args.device,
        acoustid_api_key=args.acoustid_key
    )

    # Handle Ctrl+C gracefully
    def signal_handler(sig, frame):
        listener.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    # Run the async loop
    try:
        asyncio.run(listener.run())
    except KeyboardInterrupt:
        listener.stop()


if __name__ == "__main__":
    main()
