""" SQLite database storage for Remote ID data
"""

import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


def _adapt_datetime(dt: datetime) -> str:
    """Adapt datetime to ISO format string for SQLite (always UTC)"""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def _convert_datetime(s: bytes) -> datetime:
    """Convert ISO format string from SQLite to datetime (always UTC)"""
    s = s.decode()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# Register adapters for datetime handling
sqlite3.register_adapter(datetime, _adapt_datetime)
sqlite3.register_converter("DATETIME", _convert_datetime)


class RemoteIDDatabase:
    """Manages SQLite database storage for Remote ID packets"""

    def __init__(self, db_path: str = "remoteid.db"):
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self):
        """Initialize the database schema"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS remoteid(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    mac_address TEXT,
                    uas_id TEXT,
                    session_id TEXT,
                    latitude REAL,
                    longitude REAL,
                    altitude REAL,
                    height REAL,
                    height_type INTEGER,
                    operator_id TEXT,
                    operator_latitude REAL,
                    operator_longitude REAL
                )
            """
            )
            conn.commit()

            # Migrate: Add session_id column if it doesn't exist
            cursor = conn.execute("PRAGMA table_info(remoteid)")
            columns = [row[1] for row in cursor.fetchall()]
            if "session_id" not in columns:
                conn.execute("ALTER TABLE remoteid ADD COLUMN session_id TEXT")
                conn.commit()
                logger.info("Added session_id column to existing database")

            if "height" not in columns:
                conn.execute("ALTER TABLE remoteid ADD COLUMN height REAL")
                conn.commit()
                logger.info("Added height column to existing database")

            if "height_type" not in columns:
                conn.execute("ALTER TABLE remoteid ADD COLUMN height_type INTEGER")
                conn.commit()
                logger.info("Added height_type column to existing database")

        logger.debug("Database initialized at %s", self.db_path)

    # pylint: disable=too-many-arguments
    # pylint: disable=too-many-positional-arguments
    def store(
        self,
        timestamp: datetime,
        mac_address: str,
        uas_id: str,
        latitude: float,
        longitude: float,
        altitude: float,
        height: float = None,
        height_type: int = None,
        operator_id: str = None,
        operator_latitude: float = None,
        operator_longitude: float = None,
        session_id: str = None,
    ):
        """Store a Remote ID record in the database"""
        try:
            with sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES) as conn:
                conn.execute(
                    """INSERT INTO remoteid
                       (timestamp, mac_address, uas_id, session_id, latitude, longitude, altitude,
                        height, height_type,
                        operator_id, operator_latitude, operator_longitude)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        timestamp,
                        mac_address,
                        uas_id,
                        session_id,
                        latitude,
                        longitude,
                        altitude,
                        height,
                        height_type,
                        operator_id,
                        operator_latitude,
                        operator_longitude,
                    ),
                )
                conn.commit()
            logger.debug("Stored record for UAS %s", uas_id)
        except sqlite3.Error as e:
            logger.error("Database error: %s", e)

    def get_events_after(
        self, timestamp: datetime, limit: int = 200
    ) -> List[Dict[str, Any]]:
        """Get events after a given timestamp, ordered by timestamp.

        Args:
            timestamp: Get events after this timestamp (exclusive)
            limit: Maximum number of events to return

        Returns:
            List of event dictionaries matching the API format
        """
        try:
            with sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """SELECT timestamp, mac_address, uas_id, session_id,
                              latitude, longitude, altitude,
                              height, height_type,
                              operator_id, operator_latitude, operator_longitude
                       FROM remoteid
                       WHERE timestamp > ?
                       ORDER BY timestamp ASC
                       LIMIT ?""",
                    (timestamp, limit),
                )
                rows = cursor.fetchall()

            events = []
            for row in rows:
                ts = row["timestamp"]
                ts_iso = ts.isoformat() if ts else None

                event = {
                    "timestamp": ts_iso,
                    "mac_address": row["mac_address"],
                    "uas_id": row["uas_id"],
                    "session_id": row["session_id"],
                    "latitude": row["latitude"],
                    "longitude": row["longitude"],
                    "altitude": row["altitude"],
                    "height": row["height"],
                    "height_type": row["height_type"],
                    "operator_id": row["operator_id"],
                    "operator_latitude": row["operator_latitude"],
                    "operator_longitude": row["operator_longitude"],
                }
                # Remove None values to keep payload clean
                event = {k: v for k, v in event.items() if v is not None}
                events.append(event)

            logger.debug("Retrieved %d events after %s", len(events), timestamp)
            return events
        except sqlite3.Error as e:
            logger.error("Database error in get_events_after: %s", e)
            return []

    def get_max_timestamp(self) -> Optional[datetime]:
        """Get the maximum timestamp in the database.

        Returns:
            The most recent timestamp, or None if no records exist
        """
        try:
            with sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES) as conn:
                cursor = conn.execute("SELECT MAX(timestamp) FROM remoteid")
                result = cursor.fetchone()
                max_ts = result[0] if result else None
                logger.debug("Max timestamp in database: %s", max_ts)
                return max_ts
        except sqlite3.Error as e:
            logger.error("Database error in get_max_timestamp: %s", e)
            return None

    def close(self):
        """Close database connections (no-op for sqlite3 context manager pattern)"""
