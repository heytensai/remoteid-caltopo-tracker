""" SQLite database storage for Remote ID data
"""

import sqlite3
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _adapt_datetime(dt: datetime) -> str:
    """ Adapt datetime to ISO format string for SQLite
    """
    return dt.isoformat()


def _convert_datetime(s: bytes) -> datetime:
    """ Convert ISO format string from SQLite to datetime
    """
    return datetime.fromisoformat(s.decode())


# Register adapters for datetime handling
sqlite3.register_adapter(datetime, _adapt_datetime)
sqlite3.register_converter("DATETIME", _convert_datetime)


class RemoteIDDatabase:
    """ Manages SQLite database storage for Remote ID packets
    """

    def __init__(self, db_path: str = "remoteid.db"):
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self):
        """ Initialize the database schema
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS remoteid(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    mac_address TEXT,
                    uas_id TEXT,
                    session_id TEXT,
                    latitude REAL,
                    longitude REAL,
                    altitude REAL,
                    operator_id TEXT,
                    operator_latitude REAL,
                    operator_longitude REAL
                )
            """)
            conn.commit()
        logger.debug("Database initialized at %s", self.db_path)

    # pylint: disable=too-many-arguments
    # pylint: disable=too-many-positional-arguments
    def store(self, timestamp: datetime, mac_address: str, uas_id: str,
              latitude: float, longitude: float, altitude: float,
              operator_id: str = None, operator_latitude: float = None,
              operator_longitude: float = None, session_id: str = None):
        """ Store a Remote ID record in the database
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """INSERT INTO remoteid
                       (timestamp, mac_address, uas_id, session_id, latitude, longitude, altitude,
                        operator_id, operator_latitude, operator_longitude)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (timestamp, mac_address, uas_id, session_id, latitude, longitude, altitude,
                     operator_id, operator_latitude, operator_longitude)
                )
                conn.commit()
            logger.debug("Stored record for UAS %s", uas_id)
        except sqlite3.Error as e:
            logger.error("Database error: %s", e)

    def close(self):
        """ Close database connections (no-op for sqlite3 context manager pattern)
        """
