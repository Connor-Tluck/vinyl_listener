"""Flask web app for vinyl listening dashboard."""

import json
import time
import urllib.request
import urllib.error
import urllib.parse
from flask import Flask, jsonify, render_template, Response, request

from database import (
    get_recent_listens, get_current_track, get_listen_stats, log_listen,
    search_listens, get_top_tracks, get_top_artists, get_listens_by_date,
    get_sessions, get_session_listens, toggle_star, get_starred_albums,
    get_genre_stats, check_track_in_history
)

# Discogs API configuration
DISCOGS_TOKEN = "XttbwbkRakiKYQCuKUHmUvPrBefAeyQtKnAXAtgn"
DISCOGS_USER_AGENT = "VinylID/1.0"

# Cache for tracklists to avoid repeated API calls
tracklist_cache = {}

app = Flask(__name__)

# Global state for the listener
listener_state = {
    "status": "stopped",
    "current_track": None,
    "audio_level": 0.0,
    "message": "Not running",
    "idle": False
}

# Weather cache
weather_cache = {
    "data": None,
    "fetched_at": 0
}
WEATHER_CACHE_DURATION = 900  # 15 minutes

# Display configuration for Pi mode
display_config = {
    "pi_mode": False,
    "fullscreen": False,
    "touch_mode": False
}

# Runtime configuration (can be updated via API)
runtime_config = {
    "audio_mode": "microphone",
    "audio_gain": 1.0,
    "silence_threshold": 0.001,
    "identification_interval": 15,
    "sample_duration": 15,
    "duplicate_window": 10,
    "idle_screen_enabled": True,
    "idle_timeout_minutes": 5,
    "idle_weather_location": "NYC"
}

# Callback to notify listener of config changes
config_change_callback = None


def set_config_change_callback(callback):
    """Set callback function to be called when config changes."""
    global config_change_callback
    config_change_callback = callback


def set_display_config(config: dict):
    """Set display configuration from run.py."""
    display_config.update(config)
    # Also update runtime_config with any overlapping values
    for key in ["idle_screen_enabled", "idle_timeout_minutes", "idle_weather_location"]:
        if key in config:
            runtime_config[key] = config[key]


def set_runtime_config(config: dict):
    """Set runtime configuration from run.py."""
    runtime_config.update(config)


def update_listener_state(status=None, current_track=None, audio_level=None, message=None, idle=None):
    """Update the global listener state."""
    if status is not None:
        listener_state["status"] = status
    if current_track is not None:
        listener_state["current_track"] = current_track
    if audio_level is not None:
        listener_state["audio_level"] = audio_level
    if message is not None:
        listener_state["message"] = message
    if idle is not None:
        listener_state["idle"] = idle


@app.route("/")
def index():
    """Main now playing page."""
    return render_template("index.html", display=display_config)


@app.route("/history")
def history():
    """History page."""
    return render_template("history.html", display=display_config)


@app.route("/api/status")
def api_status():
    """Get current listener status."""
    return jsonify(listener_state)


@app.route("/api/current")
def api_current():
    """Get current/most recent track."""
    track = get_current_track()
    return jsonify(track)


@app.route("/api/history")
def api_history():
    """Get listening history with optional filters."""
    query = request.args.get('q')
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    starred_only = request.args.get('starred') == '1'
    limit = request.args.get('limit', 100, type=int)

    if query or start_date or end_date or starred_only:
        listens = search_listens(query, start_date, end_date, starred_only, limit)
    else:
        listens = get_recent_listens(limit)

    return jsonify(listens)


@app.route("/api/stats")
def api_stats():
    """Get listening statistics."""
    stats = get_listen_stats()
    return jsonify(stats)


@app.route("/api/top-tracks")
def api_top_tracks():
    """Get top played tracks."""
    limit = request.args.get('limit', 10, type=int)
    tracks = get_top_tracks(limit)
    return jsonify(tracks)


@app.route("/api/top-artists")
def api_top_artists():
    """Get top played artists."""
    limit = request.args.get('limit', 10, type=int)
    artists = get_top_artists(limit)
    return jsonify(artists)


@app.route("/api/listens-by-date")
def api_listens_by_date():
    """Get listen counts by date."""
    data = get_listens_by_date()
    return jsonify(data)


@app.route("/api/sessions")
def api_sessions():
    """Get listening sessions."""
    limit = request.args.get('limit', 20, type=int)
    sessions = get_sessions(limit)
    return jsonify(sessions)


@app.route("/api/sessions/<int:session_id>")
def api_session_listens(session_id):
    """Get listens for a specific session."""
    listens = get_session_listens(session_id)
    return jsonify(listens)


@app.route("/api/star/<int:listen_id>", methods=["POST"])
def api_toggle_star(listen_id):
    """Toggle star status for a listen."""
    new_status = toggle_star(listen_id)
    return jsonify({"starred": new_status})


@app.route("/api/starred-albums")
def api_starred_albums():
    """Get albums with starred tracks."""
    albums = get_starred_albums()
    return jsonify(albums)


@app.route("/api/genres")
def api_genres():
    """Get listen counts by genre."""
    genres = get_genre_stats()
    return jsonify(genres)


@app.route("/api/check-history")
def api_check_history():
    """Check if a track has been played before."""
    track = request.args.get('track')
    artist = request.args.get('artist')
    if not track or not artist:
        return jsonify({"error": "track and artist required"}), 400
    in_history = check_track_in_history(track, artist)
    return jsonify({"in_history": in_history})


@app.route("/api/tracklist")
def api_tracklist():
    """Get tracklist for an album from Discogs."""
    artist = request.args.get('artist')
    album = request.args.get('album')

    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400

    # Check cache first
    cache_key = f"{artist}|{album}".lower()
    if cache_key in tracklist_cache:
        return jsonify(tracklist_cache[cache_key])

    try:
        # Search for the release on Discogs
        search_query = urllib.parse.quote(f"{artist} {album}")
        search_url = f"https://api.discogs.com/database/search?q={search_query}&type=release&per_page=5"

        headers = {
            "User-Agent": DISCOGS_USER_AGENT,
            "Authorization": f"Discogs token={DISCOGS_TOKEN}"
        }

        req = urllib.request.Request(search_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            search_data = json.loads(response.read().decode())

        results = search_data.get("results", [])
        if not results:
            return jsonify({"error": "Album not found", "tracks": []})

        # Get the first matching release
        release_id = results[0].get("id")

        # Fetch the release details for tracklist
        release_url = f"https://api.discogs.com/releases/{release_id}"
        req = urllib.request.Request(release_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            release_data = json.loads(response.read().decode())

        tracklist = release_data.get("tracklist", [])
        tracks = []
        for t in tracklist:
            # Skip headings (type_ == "heading")
            if t.get("type_") == "heading":
                continue
            tracks.append({
                "position": t.get("position", ""),
                "title": t.get("title", ""),
                "duration": t.get("duration", "")
            })

        # Get community stats
        community = release_data.get("community", {})
        rating = community.get("rating", {})

        # Get videos
        videos = release_data.get("videos", [])
        video_list = []
        for v in videos:
            if v.get("embed", False):
                video_list.append({
                    "title": v.get("title", ""),
                    "url": v.get("uri", ""),
                    "duration": v.get("duration", 0)
                })

        result = {
            "album": release_data.get("title", album),
            "artist": release_data.get("artists_sort", artist),
            "year": release_data.get("year"),
            "tracks": tracks,
            "videos": video_list,
            "stats": {
                "have": community.get("have", 0),
                "want": community.get("want", 0),
                "rating": rating.get("average", 0),
                "rating_count": rating.get("count", 0)
            }
        }

        # Cache the result
        tracklist_cache[cache_key] = result

        return jsonify(result)

    except (urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
        return jsonify({"error": str(e), "tracks": []})


@app.route("/api/videos")
def api_videos():
    """Get music videos from cached tracklist data."""
    limit = request.args.get('limit', 20, type=int)

    # Collect videos from all cached tracklist data
    videos = []
    for cache_key, data in tracklist_cache.items():
        if isinstance(data, dict) and "videos" in data:
            artist = data.get("artist", "")
            for v in data.get("videos", []):
                videos.append({
                    "title": v.get("title", ""),
                    "url": v.get("url", ""),
                    "artist": artist
                })

    # If no cached videos, try to fetch for top tracks
    if not videos:
        tracks = get_top_tracks(5)
        headers = {
            "User-Agent": DISCOGS_USER_AGENT,
            "Authorization": f"Discogs token={DISCOGS_TOKEN}"
        }

        for track_data in tracks:
            artist = track_data.get("artist", "")
            album = track_data.get("album", "")
            if not artist or not album:
                continue

            cache_key = f"{artist}|{album}".lower()
            if cache_key in tracklist_cache:
                continue

            try:
                search_query = urllib.parse.quote(f"{artist} {album}")
                search_url = f"https://api.discogs.com/database/search?q={search_query}&type=release&per_page=1"

                req = urllib.request.Request(search_url, headers=headers)
                with urllib.request.urlopen(req, timeout=5) as response:
                    search_data = json.loads(response.read().decode())

                results = search_data.get("results", [])
                if not results:
                    continue

                release_id = results[0].get("id")
                release_url = f"https://api.discogs.com/releases/{release_id}"
                req = urllib.request.Request(release_url, headers=headers)
                with urllib.request.urlopen(req, timeout=5) as response:
                    release_data = json.loads(response.read().decode())

                for v in release_data.get("videos", []):
                    if v.get("embed", False):
                        videos.append({
                            "title": v.get("title", ""),
                            "url": v.get("uri", ""),
                            "artist": artist
                        })

                # Only fetch a couple to avoid timeouts
                if len(videos) >= 5:
                    break

            except:
                continue

    return jsonify(videos[:limit])


@app.route("/api/weather")
def api_weather():
    """Get weather data for idle screen."""
    location = display_config.get("idle_weather_location", "NYC")

    # Check cache
    now = time.time()
    if weather_cache["data"] and (now - weather_cache["fetched_at"]) < WEATHER_CACHE_DURATION:
        return jsonify(weather_cache["data"])

    try:
        url = f"https://wttr.in/{location}?format=j1"
        req = urllib.request.Request(url, headers={"User-Agent": "vinyl-id"})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())

        current = data.get("current_condition", [{}])[0]
        weather_data = {
            "temp_f": current.get("temp_F", ""),
            "temp_c": current.get("temp_C", ""),
            "condition": current.get("weatherDesc", [{}])[0].get("value", ""),
            "icon": current.get("weatherCode", ""),
            "location": location
        }

        # Update cache
        weather_cache["data"] = weather_data
        weather_cache["fetched_at"] = now

        return jsonify(weather_data)
    except (urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config")
def api_get_config():
    """Get current runtime configuration."""
    return jsonify(runtime_config)


@app.route("/api/config", methods=["POST"])
def api_update_config():
    """Update runtime configuration."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    updated = []
    for key, value in data.items():
        if key in runtime_config:
            # Validate and convert types
            if key in ["audio_gain", "silence_threshold"]:
                try:
                    value = float(value)
                except (ValueError, TypeError):
                    continue
            elif key in ["identification_interval", "sample_duration", "duplicate_window", "idle_timeout_minutes"]:
                try:
                    value = int(value)
                except (ValueError, TypeError):
                    continue
            elif key == "idle_screen_enabled":
                value = bool(value)
            elif key == "audio_mode":
                if value not in ["microphone", "line_in"]:
                    continue

            runtime_config[key] = value
            updated.append(key)

            # Also update display_config for idle settings
            if key in ["idle_screen_enabled", "idle_timeout_minutes", "idle_weather_location"]:
                display_config[key] = value

    # Notify listener of changes if callback is set
    if updated and config_change_callback:
        config_change_callback(runtime_config)

    return jsonify({"updated": updated, "config": runtime_config})


@app.route("/api/events")
def api_events():
    """Server-sent events for real-time updates."""
    def generate():
        last_track_id = None
        try:
            while True:
                data = {
                    "status": listener_state["status"],
                    "audio_level": listener_state["audio_level"],
                    "message": listener_state["message"],
                    "current_track": listener_state["current_track"],
                    "idle": listener_state["idle"],
                    "idle_config": {
                        "enabled": display_config.get("idle_screen_enabled", False),
                        "timeout_minutes": display_config.get("idle_timeout_minutes", 10),
                        "location": display_config.get("idle_weather_location", "NYC")
                    }
                }

                current = get_current_track()
                if current and current.get("id") != last_track_id:
                    last_track_id = current.get("id")
                    data["new_track"] = current

                yield f"data: {json.dumps(data)}\n\n"
                time.sleep(1)  # Slower updates to reduce load
        except GeneratorExit:
            pass  # Client disconnected

    response = Response(generate(), mimetype="text/event-stream")
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Connection'] = 'keep-alive'
    response.headers['X-Accel-Buffering'] = 'no'
    return response


if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True)
