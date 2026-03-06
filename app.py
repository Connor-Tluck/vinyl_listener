"""Flask web app for vinyl listening dashboard."""

import json
import time
import urllib.request
import urllib.error
from flask import Flask, jsonify, render_template, Response, request

from database import (
    get_recent_listens, get_current_track, get_listen_stats, log_listen,
    search_listens, get_top_tracks, get_top_artists, get_listens_by_date,
    get_sessions, get_session_listens, toggle_star, get_starred_albums
)

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


def set_display_config(config: dict):
    """Set display configuration from run.py."""
    display_config.update(config)


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
