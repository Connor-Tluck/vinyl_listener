# Vinyl ID

A real-time vinyl record identifier that listens to your turntable and automatically identifies tracks using Shazam. Features a web-based dashboard to view what's currently playing and browse your listening history.

![Now Playing](app_images/screenshot_listener_ui.png)

## Features

- **Real-time identification** - Continuously listens and identifies tracks every 15 seconds
- **Album art display** - Shows album artwork with dynamic background colors
- **Listening history** - Logs all identified tracks to a local database
- **Calendar view** - Browse history by date with an interactive calendar
- **Top charts** - See your most played tracks and artists
- **Search & filter** - Search your history by track, artist, or album
- **Duplicate prevention** - Won't log the same track twice within 10 minutes

![History](app_images/screenshot_history_ui.png)

## Installation

```bash
cd vinyl-id

# Install Python dependencies
pip install -r requirements.txt

# Optional: Install chromaprint for AcoustID fallback (macOS)
brew install chromaprint
```

## Usage

```bash
# Start the app
python run.py --port 8080

# Open in browser
open http://localhost:8080
```

### Command Line Options

```bash
python run.py --help

Options:
  --port PORT        Web server port (default: 5000)
  --device DEVICE    Audio input device ID
  --interval SECS    Identification interval in seconds (default: 15)
  --list-devices     List available audio input devices
```

### Selecting an Audio Device

```bash
# List available devices
python run.py --list-devices

# Use a specific device (e.g., USB audio interface)
python run.py --device 2 --port 8080
```

## How It Works

1. Captures audio continuously from your microphone or line-in
2. Every 15 seconds, samples the audio and sends it to Shazam for identification
3. If Shazam fails, falls back to AcoustID (MusicBrainz database)
4. Logs identified tracks to a local SQLite database
5. Displays results in a web dashboard with album art

## Project Structure

```
vinyl-id/
├── run.py              # Main entry point - starts web server and listener
├── app.py              # Flask web application
├── identifier.py       # Shazam + AcoustID identification
├── database.py         # SQLite database operations
├── vinyl_listener.py   # CLI-only listener (standalone)
├── templates/
│   ├── index.html      # Now Playing page
│   └── history.html    # Listening History page
├── vinyl_history.db    # SQLite database (created on first run)
└── requirements.txt    # Python dependencies
```

## Requirements

- Python 3.10+
- Working microphone or audio input
- Internet connection (for Shazam API)

## Cost

**$0/month** - All APIs used are free:
- Shazam (via shazamio) - Free
- AcoustID/MusicBrainz - Free
- SQLite - Local storage

## Database

Listening history is stored locally in `vinyl_history.db`. Back it up by copying the file:

```bash
cp vinyl_history.db vinyl_history_backup.db
```
