"""Database module for storing vinyl listening history."""

import sqlite3
from datetime import datetime
from typing import Optional
from pathlib import Path

DB_PATH = Path(__file__).parent / "vinyl_history.db"


def get_connection() -> sqlite3.Connection:
    """Get database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize the database schema."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track TEXT NOT NULL,
            artist TEXT NOT NULL,
            album TEXT,
            year TEXT,
            source TEXT,
            confidence REAL,
            cover_url TEXT,
            session_id INTEGER,
            starred INTEGER DEFAULT 0,
            listened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP,
            track_count INTEGER DEFAULT 0
        )
    """)
    # Add columns if they don't exist (for existing databases)
    try:
        conn.execute("ALTER TABLE listens ADD COLUMN cover_url TEXT")
    except:
        pass
    try:
        conn.execute("ALTER TABLE listens ADD COLUMN session_id INTEGER")
    except:
        pass
    try:
        conn.execute("ALTER TABLE listens ADD COLUMN starred INTEGER DEFAULT 0")
    except:
        pass
    conn.commit()
    conn.close()


def log_listen(
    track: str,
    artist: str,
    album: Optional[str] = None,
    year: Optional[str] = None,
    source: Optional[str] = None,
    confidence: Optional[float] = None,
    cover_url: Optional[str] = None
) -> Optional[int]:
    """Log a listen to the database. Returns the listen ID, or None if duplicate."""
    conn = get_connection()

    # Check if this track was logged in the last 10 minutes (avoid duplicates)
    cursor = conn.execute(
        """
        SELECT id FROM listens
        WHERE track = ? AND artist = ?
        AND listened_at > datetime('now', '-10 minutes')
        ORDER BY listened_at DESC
        LIMIT 1
        """,
        (track, artist)
    )
    existing = cursor.fetchone()
    if existing:
        conn.close()
        return None  # Duplicate, don't save

    session_id = get_current_session_id()
    cursor = conn.execute(
        """
        INSERT INTO listens (track, artist, album, year, source, confidence, cover_url, session_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (track, artist, album, year, source, confidence, cover_url, session_id)
    )
    listen_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return listen_id


def get_recent_listens(limit: int = 50, starred_only: bool = False) -> list[dict]:
    """Get recent listens from the database."""
    conn = get_connection()
    sql = """
        SELECT id, track, artist, album, year, source, confidence, cover_url, starred, listened_at
        FROM listens
    """
    if starred_only:
        sql += " WHERE starred = 1"
    sql += " ORDER BY listened_at DESC LIMIT ?"

    cursor = conn.execute(sql, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_current_track() -> Optional[dict]:
    """Get the most recent listen."""
    conn = get_connection()
    cursor = conn.execute(
        """
        SELECT id, track, artist, album, year, source, confidence, cover_url, starred, listened_at
        FROM listens
        ORDER BY listened_at DESC
        LIMIT 1
        """
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def search_listens(
    query: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    starred_only: bool = False,
    limit: int = 100
) -> list[dict]:
    """Search listens with filters."""
    conn = get_connection()

    sql = """
        SELECT id, track, artist, album, year, source, confidence, cover_url, starred, listened_at
        FROM listens
        WHERE 1=1
    """
    params = []

    if starred_only:
        sql += " AND starred = 1"

    if query:
        sql += " AND (track LIKE ? OR artist LIKE ? OR album LIKE ?)"
        like_query = f"%{query}%"
        params.extend([like_query, like_query, like_query])

    if start_date:
        sql += " AND date(listened_at) >= date(?)"
        params.append(start_date)

    if end_date:
        sql += " AND date(listened_at) <= date(?)"
        params.append(end_date)

    sql += " ORDER BY listened_at DESC LIMIT ?"
    params.append(limit)

    cursor = conn.execute(sql, params)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_top_tracks(limit: int = 10) -> list[dict]:
    """Get most played tracks."""
    conn = get_connection()
    cursor = conn.execute(
        """
        SELECT track, artist, album, cover_url, COUNT(*) as play_count
        FROM listens
        GROUP BY track, artist
        ORDER BY play_count DESC
        LIMIT ?
        """,
        (limit,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_top_artists(limit: int = 10) -> list[dict]:
    """Get most played artists."""
    conn = get_connection()
    cursor = conn.execute(
        """
        SELECT artist, COUNT(*) as play_count, COUNT(DISTINCT track) as unique_tracks
        FROM listens
        GROUP BY artist
        ORDER BY play_count DESC
        LIMIT ?
        """,
        (limit,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_listens_by_date() -> list[dict]:
    """Get listen counts grouped by date."""
    conn = get_connection()
    cursor = conn.execute(
        """
        SELECT date(listened_at) as date, COUNT(*) as count
        FROM listens
        GROUP BY date(listened_at)
        ORDER BY date DESC
        LIMIT 30
        """
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_listen_stats() -> dict:
    """Get listening statistics."""
    conn = get_connection()

    total = conn.execute("SELECT COUNT(*) FROM listens").fetchone()[0]
    unique_tracks = conn.execute("SELECT COUNT(DISTINCT track || artist) FROM listens").fetchone()[0]
    unique_artists = conn.execute("SELECT COUNT(DISTINCT artist) FROM listens").fetchone()[0]

    top_artists = conn.execute(
        """
        SELECT artist, COUNT(*) as count
        FROM listens
        GROUP BY artist
        ORDER BY count DESC
        LIMIT 5
        """
    ).fetchall()

    conn.close()

    return {
        "total_listens": total,
        "unique_tracks": unique_tracks,
        "unique_artists": unique_artists,
        "top_artists": [{"artist": row[0], "count": row[1]} for row in top_artists]
    }


# Session management
_current_session_id: Optional[int] = None


def start_session() -> int:
    """Start a new listening session."""
    global _current_session_id
    conn = get_connection()
    cursor = conn.execute("INSERT INTO sessions DEFAULT VALUES")
    _current_session_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return _current_session_id


def get_current_session_id() -> Optional[int]:
    """Get current session ID, starting one if needed."""
    global _current_session_id
    if _current_session_id is None:
        _current_session_id = start_session()
    return _current_session_id


def end_session():
    """End the current listening session."""
    global _current_session_id
    if _current_session_id:
        conn = get_connection()
        conn.execute(
            """
            UPDATE sessions
            SET ended_at = CURRENT_TIMESTAMP,
                track_count = (SELECT COUNT(*) FROM listens WHERE session_id = ?)
            WHERE id = ?
            """,
            (_current_session_id, _current_session_id)
        )
        conn.commit()
        conn.close()
    _current_session_id = None


def get_sessions(limit: int = 20) -> list[dict]:
    """Get recent listening sessions."""
    conn = get_connection()
    cursor = conn.execute(
        """
        SELECT
            s.id,
            s.started_at,
            s.ended_at,
            COUNT(l.id) as track_count,
            MIN(l.listened_at) as first_track,
            MAX(l.listened_at) as last_track
        FROM sessions s
        LEFT JOIN listens l ON l.session_id = s.id
        GROUP BY s.id
        HAVING track_count > 0
        ORDER BY s.started_at DESC
        LIMIT ?
        """,
        (limit,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_session_listens(session_id: int) -> list[dict]:
    """Get all listens for a specific session."""
    conn = get_connection()
    cursor = conn.execute(
        """
        SELECT id, track, artist, album, year, source, confidence, cover_url, listened_at
        FROM listens
        WHERE session_id = ?
        ORDER BY listened_at ASC
        """,
        (session_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def toggle_star(listen_id: int) -> bool:
    """Toggle starred status for a listen. Returns new starred state."""
    conn = get_connection()
    cursor = conn.execute("SELECT starred FROM listens WHERE id = ?", (listen_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False

    new_starred = 0 if row[0] else 1
    conn.execute("UPDATE listens SET starred = ? WHERE id = ?", (new_starred, listen_id))
    conn.commit()
    conn.close()
    return bool(new_starred)


def star_album(album: str, artist: str, starred: bool = True) -> int:
    """Star/unstar all tracks from an album. Returns count of updated tracks."""
    conn = get_connection()
    cursor = conn.execute(
        "UPDATE listens SET starred = ? WHERE album = ? AND artist = ?",
        (1 if starred else 0, album, artist)
    )
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count


def get_starred_albums() -> list[dict]:
    """Get albums that have starred tracks."""
    conn = get_connection()
    cursor = conn.execute(
        """
        SELECT DISTINCT album, artist, cover_url, year,
               COUNT(*) as track_count,
               SUM(starred) as starred_count
        FROM listens
        WHERE album IS NOT NULL
        GROUP BY album, artist
        HAVING starred_count > 0
        ORDER BY MAX(listened_at) DESC
        """
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


# Initialize database on import
init_db()
