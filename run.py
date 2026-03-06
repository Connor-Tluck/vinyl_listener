#!/usr/bin/env python3
"""
Unified runner for Vinyl ID - starts both the web server and audio listener.

Usage:
    python run.py [--config CONFIG_FILE] [--device DEVICE_ID] [--port PORT]
"""

import argparse
import asyncio
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

import numpy as np
import sounddevice as sd
import yaml

from identifier import HybridIdentifier, IdentificationResult
from database import log_listen, init_db
from app import app, update_listener_state, set_display_config


# Default configuration
DEFAULT_CONFIG = {
    # Audio settings
    "audio_mode": "microphone",  # "line_in" or "microphone"
    "audio_device": None,
    "audio_gain": 1.0,
    "silence_threshold": 0.001,

    # Identification settings
    "identification_interval": 15,
    "sample_duration": 15,
    "duplicate_window": 10,

    # Web server settings
    "port": 8080,
    "host": "0.0.0.0",

    # Display settings
    "pi_mode": False,
    "fullscreen": False,
    "touch_mode": False,

    # Idle screen settings
    "idle_screen_enabled": True,
    "idle_timeout_minutes": 5,
    "idle_weather_location": "NYC",
}


def load_config(config_path: Optional[str] = None) -> dict:
    """Load configuration from YAML file, falling back to defaults."""
    config = DEFAULT_CONFIG.copy()

    # Look for config file
    if config_path:
        config_file = Path(config_path)
    else:
        # Check default locations
        for path in ["config.yaml", "config.yml"]:
            if Path(path).exists():
                config_file = Path(path)
                break
        else:
            config_file = None

    if config_file and config_file.exists():
        print(f"Loading config from: {config_file}")
        with open(config_file) as f:
            file_config = yaml.safe_load(f) or {}
            config.update(file_config)

    return config


def resolve_audio_device(device_spec: Union[None, int, str]) -> Optional[int]:
    """Resolve audio device from name or ID."""
    if device_spec is None:
        return None

    if isinstance(device_spec, int):
        return device_spec

    # Search by name (partial match)
    devices = sd.query_devices()
    device_spec_lower = device_spec.lower()

    for i, device in enumerate(devices):
        if device['max_input_channels'] > 0:
            if device_spec_lower in device['name'].lower():
                print(f"Found audio device: [{i}] {device['name']}")
                return i

    print(f"Warning: Audio device '{device_spec}' not found, using default")
    return None


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
        acoustid_api_key: Optional[str] = None,
        audio_gain: float = 1.0,
        silence_threshold: float = 0.001,
        audio_mode: str = "microphone",
        idle_timeout_minutes: float = 10.0,
        idle_screen_enabled: bool = True
    ):
        self.identification_interval = identification_interval
        self.buffer_duration = buffer_duration
        self.sample_duration = sample_duration
        self.device = device
        self.audio_gain = audio_gain
        self.silence_threshold = silence_threshold
        self.audio_mode = audio_mode
        self.idle_timeout_minutes = idle_timeout_minutes
        self.idle_screen_enabled = idle_screen_enabled

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

        # Idle screen tracking
        self.silence_started_at: Optional[float] = None
        self.is_idle = False

    def _audio_callback(self, indata, frames, time_info, status):
        """Callback for audio stream."""
        if status:
            print(f"Audio status: {status}", file=sys.stderr)

        with self.lock:
            samples = indata.flatten()

            # Apply gain adjustment
            if self.audio_gain != 1.0:
                samples = samples * self.audio_gain
                # Clip to prevent distortion
                samples = np.clip(samples, -1.0, 1.0)

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
            if rms < self.silence_threshold:
                # Track silence duration
                if self.silence_started_at is None:
                    self.silence_started_at = time.time()

                # Check if we should enter idle mode
                if self.idle_screen_enabled:
                    silence_duration = time.time() - self.silence_started_at
                    if silence_duration >= (self.idle_timeout_minutes * 60):
                        if not self.is_idle:
                            self.is_idle = True
                            update_listener_state(message="Listening (quiet)", idle=True)
                    else:
                        update_listener_state(message="Listening (quiet)", idle=False)
                else:
                    update_listener_state(message="Listening (quiet)")
                continue

            # Audio detected - reset silence tracking
            if self.silence_started_at is not None:
                self.silence_started_at = None
                if self.is_idle:
                    self.is_idle = False
                    update_listener_state(idle=False)

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
                        cover_url=result.cover_url,
                        track_number=result.track_number,
                        genre=result.genre
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

        print(f"Audio mode: {self.audio_mode}")
        if self.audio_gain != 1.0:
            print(f"Gain: {self.audio_gain}x")
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


def run_flask(port: int, host: str = "0.0.0.0"):
    """Run Flask in a separate thread."""
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)


def main():
    parser = argparse.ArgumentParser(description="Vinyl ID - Web-based vinyl record identifier")
    parser.add_argument("--config", type=str, default=None, help="Path to config file (default: config.yaml)")
    parser.add_argument("--device", type=str, default=None, help="Audio input device ID or name")
    parser.add_argument("--port", type=int, default=None, help="Web server port")
    parser.add_argument("--interval", type=float, default=None, help="Identification interval in seconds")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    parser.add_argument("--pi-mode", action="store_true", help="Enable Pi/kiosk mode")

    args = parser.parse_args()

    if args.list_devices:
        print("\nAvailable audio input devices:")
        print("-" * 50)
        devices = sd.query_devices()
        for i, device in enumerate(devices):
            if device['max_input_channels'] > 0:
                marker = " (default)" if i == sd.default.device[0] else ""
                print(f"  [{i}] {device['name']}{marker}")
        print("\nTip: Use device name in config.yaml, e.g.:")
        print('  audio_device: "USB Audio"')
        return

    # Load config file
    config = load_config(args.config)

    # Command line args override config file
    if args.device is not None:
        # Try to parse as int, otherwise use as string
        try:
            config["audio_device"] = int(args.device)
        except ValueError:
            config["audio_device"] = args.device
    if args.port is not None:
        config["port"] = args.port
    if args.interval is not None:
        config["identification_interval"] = args.interval
    if args.pi_mode:
        config["pi_mode"] = True
        config["fullscreen"] = True
        config["touch_mode"] = True

    # Resolve audio device name to ID
    device_id = resolve_audio_device(config["audio_device"])

    # Initialize database
    init_db()

    # Set display config for templates
    set_display_config({
        "pi_mode": config["pi_mode"],
        "fullscreen": config["fullscreen"],
        "touch_mode": config["touch_mode"],
        "idle_screen_enabled": config["idle_screen_enabled"],
        "idle_weather_location": config["idle_weather_location"],
        "idle_timeout_minutes": config["idle_timeout_minutes"]
    })

    # Create listener
    listener = WebAudioListener(
        identification_interval=config["identification_interval"],
        sample_duration=config["sample_duration"],
        device=device_id,
        audio_gain=config["audio_gain"],
        silence_threshold=config["silence_threshold"],
        audio_mode=config["audio_mode"],
        idle_timeout_minutes=config["idle_timeout_minutes"],
        idle_screen_enabled=config["idle_screen_enabled"]
    )

    # Handle Ctrl+C
    def signal_handler(sig, frame):
        listener.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    # Start Flask in background thread
    flask_thread = threading.Thread(
        target=run_flask,
        args=(config["port"], config["host"]),
        daemon=True
    )
    flask_thread.start()

    print(f"\nWeb UI: http://localhost:{config['port']}")
    if config["pi_mode"]:
        print("Pi mode: enabled")
    print("Press Ctrl+C to stop\n")

    # Run async listener
    try:
        asyncio.run(listener.run())
    except KeyboardInterrupt:
        listener.stop()


if __name__ == "__main__":
    main()
