"""Database layer for web interface"""

import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _adapt_datetime(dt: datetime) -> str:
    """Adapt datetime to ISO format string for SQLite"""
    return dt.isoformat()


def _convert_datetime(s: bytes) -> datetime:
    """Convert ISO format string from SQLite to datetime"""
    return datetime.fromisoformat(s.decode())


# Register adapters for datetime handling
sqlite3.register_adapter(datetime, _adapt_datetime)
sqlite3.register_converter("DATETIME", _convert_datetime)


class WebDatabase:
    """Manages SQLite database for web interface"""

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self):
        """Initialize the database schema"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES) as conn:
            # Enable WAL mode for better concurrent access
            conn.execute("PRAGMA journal_mode=WAL")

            # Create remoteid table with source column
            conn.execute("""
                CREATE TABLE IF NOT EXISTS remoteid(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT,
                    timestamp DATETIME,
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

            # Create sync log table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT,
                    last_sync DATETIME,
                    records_imported INTEGER
                )
            """)

            # Create indexes
            conn.execute("CREATE INDEX IF NOT EXISTS idx_uas_time ON remoteid(uas_id, timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_source ON remoteid(source)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON remoteid(timestamp)")

            conn.commit()
        logger.debug("Database initialized at %s", self.db_path)

    def import_from_collector(self, source_db_path: str, source_name: str) -> int:
        """Import new records from a collector's database"""
        count = 0
        try:
            # Get last sync time for this source
            last_sync = self._get_last_sync(source_name)

            # Connect to source database and query new records
            columns = "id, timestamp, mac_address, uas_id, session_id, latitude, longitude, altitude, operator_id, operator_latitude, operator_longitude"

            with sqlite3.connect(source_db_path, detect_types=sqlite3.PARSE_DECLTYPES) as src_conn:
                if last_sync:
                    cursor = src_conn.execute(
                        f"SELECT {columns} FROM remoteid WHERE timestamp > ? ORDER BY timestamp",
                        (last_sync,)
                    )
                else:
                    cursor = src_conn.execute(f"SELECT {columns} FROM remoteid ORDER BY timestamp")

                # Import into web database
                # Explicitly map columns to avoid order mismatches
                with sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES) as dest_conn:
                    for row in cursor:
                        # row indices from source: id[0], timestamp[1], mac[2], uas_id[3],
                        # session_id[4], lat[5], lon[6], alt[7], op_id[8], op_lat[9], op_lon[10]

                        # Skip if already exists (check uas_id + timestamp)
                        existing = dest_conn.execute(
                            "SELECT 1 FROM remoteid WHERE uas_id = ? AND timestamp = ?",
                            (row[3], row[1])
                        ).fetchone()

                        if not existing:
                            dest_conn.execute("""
                                INSERT INTO remoteid
                                (source, timestamp, mac_address, uas_id, session_id,
                                 latitude, longitude, altitude, operator_id,
                                 operator_latitude, operator_longitude)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                source_name,      # source
                                row[1],           # timestamp
                                row[2],           # mac_address
                                row[3],           # uas_id
                                row[4] if len(row) > 4 else None,  # session_id
                                row[5] if len(row) > 5 else None,  # latitude
                                row[6] if len(row) > 6 else None,  # longitude
                                row[7] if len(row) > 7 else None,  # altitude
                                row[8] if len(row) > 8 else None,  # operator_id
                                row[9] if len(row) > 9 else None,  # operator_latitude
                                row[10] if len(row) > 10 else None  # operator_longitude
                            ))
                            count += 1

                    dest_conn.commit()

            # Update sync log
            self._update_sync_log(source_name, count)
            logger.info("Imported %d records from %s", count, source_name)
            return count

        except sqlite3.Error as e:
            logger.error("Database import error from %s: %s", source_name, e)
            return 0

    def _get_last_sync(self, source_name: str) -> Optional[datetime]:
        """Get the last sync time for a source"""
        with sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES) as conn:
            cursor = conn.execute(
                "SELECT last_sync FROM sync_log WHERE source = ? ORDER BY last_sync DESC LIMIT 1",
                (source_name,)
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def _update_sync_log(self, source_name: str, count: int):
        """Update the sync log for a source"""
        with sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES) as conn:
            conn.execute(
                "INSERT INTO sync_log (source, last_sync, records_imported) VALUES (?, ?, ?)",
                (source_name, datetime.now(), count)
            )
            conn.commit()

    def get_drones(self, start_time: datetime, end_time: datetime) -> List[Dict]:
        """Get list of unique drones seen in time window with latest positions"""
        with sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES) as conn:
            conn.row_factory = sqlite3.Row

            # Get latest position for each drone in time window
            cursor = conn.execute("""
                SELECT r1.uas_id, r1.latitude, r1.longitude, r1.altitude,
                       r1.timestamp, r1.operator_id, r1.operator_latitude, r1.operator_longitude,
                       r1.source
                FROM remoteid r1
                INNER JOIN (
                    SELECT uas_id, MAX(timestamp) as max_ts
                    FROM remoteid
                    WHERE timestamp BETWEEN ? AND ?
                    GROUP BY uas_id
                ) r2 ON r1.uas_id = r2.uas_id AND r1.timestamp = r2.max_ts
                ORDER BY r1.uas_id
            """, (start_time, end_time))

            return [dict(row) for row in cursor.fetchall()]

    def get_positions(self, start_time: datetime, end_time: datetime,
                      uas_id: Optional[str] = None, limit: int = 5000) -> List[Dict]:
        """Get positions within time window"""
        with sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES) as conn:
            conn.row_factory = sqlite3.Row

            if uas_id:
                cursor = conn.execute("""
                    SELECT * FROM remoteid
                    WHERE uas_id = ? AND timestamp BETWEEN ? AND ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, (uas_id, start_time, end_time, limit))
            else:
                cursor = conn.execute("""
                    SELECT * FROM remoteid
                    WHERE timestamp BETWEEN ? AND ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, (start_time, end_time, limit))

            return [dict(row) for row in cursor.fetchall()]

    def get_track(self, uas_id: str, start_time: datetime, end_time: datetime) -> List[Dict]:
        """Get track (ordered positions) for a specific drone"""
        with sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES) as conn:
            conn.row_factory = sqlite3.Row

            cursor = conn.execute("""
                SELECT latitude, longitude, altitude, timestamp
                FROM remoteid
                WHERE uas_id = ? AND timestamp BETWEEN ? AND ?
                ORDER BY timestamp ASC
            """, (uas_id, start_time, end_time))

            return [dict(row) for row in cursor.fetchall()]

    def get_operators(self, start_time: datetime, end_time: datetime) -> List[Dict]:
        """Get latest operator positions for drones in time window"""
        with sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES) as conn:
            conn.row_factory = sqlite3.Row

            cursor = conn.execute("""
                SELECT r1.uas_id, r1.operator_id, r1.operator_latitude,
                       r1.operator_longitude, r1.timestamp
                FROM remoteid r1
                INNER JOIN (
                    SELECT uas_id, MAX(timestamp) as max_ts
                    FROM remoteid
                    WHERE timestamp BETWEEN ? AND ?
                    AND operator_latitude IS NOT NULL
                    AND operator_latitude != 0
                    GROUP BY uas_id
                ) r2 ON r1.uas_id = r2.uas_id AND r1.timestamp = r2.max_ts
                ORDER BY r1.uas_id
            """, (start_time, end_time))

            return [dict(row) for row in cursor.fetchall()]

    def get_bounds(self, start_time: datetime, end_time: datetime) -> Optional[Tuple]:
        """Get bounding box of all positions in time window"""
        with sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES) as conn:
            cursor = conn.execute("""
                SELECT MIN(latitude), MAX(latitude), MIN(longitude), MAX(longitude)
                FROM remoteid
                WHERE timestamp BETWEEN ? AND ?
            """, (start_time, end_time))

            row = cursor.fetchone()
            if row and row[0] is not None:
                return row
            return None
