#!/usr/bin/env python3
"""Clear the Vinyl ID listening history database."""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "vinyl_history.db"


def get_stats():
    """Get current database stats."""
    if not DB_PATH.exists():
        return None

    conn = sqlite3.connect(DB_PATH)
    total_listens = conn.execute("SELECT COUNT(*) FROM listens").fetchone()[0]
    unique_tracks = conn.execute("SELECT COUNT(DISTINCT track || artist) FROM listens").fetchone()[0]
    unique_artists = conn.execute("SELECT COUNT(DISTINCT artist) FROM listens").fetchone()[0]
    starred_count = conn.execute("SELECT COUNT(*) FROM listens WHERE starred = 1").fetchone()[0]
    conn.close()

    return {
        "total_listens": total_listens,
        "unique_tracks": unique_tracks,
        "unique_artists": unique_artists,
        "starred_count": starred_count
    }


def clear_history():
    """Clear all listening history."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM listens")
    conn.execute("DELETE FROM sessions")
    conn.commit()
    conn.close()


def main():
    print("\n" + "=" * 50)
    print("  VINYL ID - Clear Listening History")
    print("=" * 50 + "\n")

    stats = get_stats()

    if stats is None:
        print("Database not found. Nothing to clear.")
        return

    if stats["total_listens"] == 0:
        print("Database is already empty. Nothing to clear.")
        return

    print("Current database contains:")
    print(f"  - {stats['total_listens']} total listens")
    print(f"  - {stats['unique_tracks']} unique tracks")
    print(f"  - {stats['unique_artists']} unique artists")
    print(f"  - {stats['starred_count']} starred tracks")
    print()

    print("WARNING: This will permanently delete ALL listening history!")
    print()

    response = input("Are you sure you want to clear all history? (yes/no): ").strip().lower()

    if response == "yes":
        clear_history()
        print("\nListening history has been cleared.")
    else:
        print("\nCancelled. No changes made.")


if __name__ == "__main__":
    main()
