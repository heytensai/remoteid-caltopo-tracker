"""Report the collector position via /api/submit/ping using a local gpsd.

A local gpsd daemon owns the USB GPS receiver and fans its output out to any
number of clients over TCP (default 127.0.0.1:2947). Speaking the
line-delimited JSON protocol directly lets every decoder instance share one
receiver without each process opening the serial device.

TPV reports carry the fix (time/lat/lon/alt/mode/status); SKY reports carry
satellite diagnostics (in view, used, signal strength) for the status line.
The fix is consumed by the API client threads in decoder.py to tag the
collector ping with the receiver's latitude/longitude.

Can also be run standalone to print the current location for troubleshooting:

    python gps.py --config config.yaml

The gps section of the config file provides the gpsd host and port. Explicit
command line arguments override the config file, which falls back to
127.0.0.1:2947. If gpsd cannot be reached at startup, GPS support is disabled
with a warning. Add --verbose to log every report plus a periodic status line
(satellites in view, signal strength) and --timeout to bound how long to wait
for a fix.
"""

import argparse
import json
import logging
import math
import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

STATUS_INTERVAL = 10.0
RECONNECT_DELAY = 5.0
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 2947
READ_TIMEOUT = 5.0

# gpsd TPV statuses that represent real satellite fixes, mirroring the old
# NMEA GGA quality 1-5 rule. Excludes NO_FIX(0), DR(5), GNSSDR(6), TIME(8)
# and SIM(9) so we never report a stale or fabricated position.
_FIX_STATUSES = frozenset({1, 2, 3, 4, 10})
_FIX_STATUS_NAMES = frozenset({"FIX", "DGPS", "RTK_FIX", "RTK_FLOAT", "PPS_FIX"})


@dataclass(frozen=True)
class GpsFix:
    """A valid GPS position fix."""

    lat: float
    lon: float
    altitude: Optional[float] = None
    timestamp: Optional[datetime] = None


def _tpv_status_accepted(status) -> bool:
    """Return True if a gpsd TPV status represents a real satellite fix.

    Newer gpsd emits status as a string like "STATUS_DGPS"; older daemons
    emit a numeric code. Both are accepted; when the field is absent the
    mode alone is treated as authoritative.
    """
    if status is None:
        return True
    if isinstance(status, bool):
        return False
    if isinstance(status, int):
        return status in _FIX_STATUSES
    if isinstance(status, str):
        name = status.upper().replace("STATUS_", "")
        return name in _FIX_STATUS_NAMES
    return False


def _tpv_time(value) -> Optional[datetime]:
    """Build a UTC datetime from a gpsd TPV 'time' field."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _finite(value) -> Optional[float]:
    """Return a finite float, or None for NaN/inf/unparseable values.

    gpsd emits "NaN" as a quoted string for unknown measurements, which
    float() happily turns into math.nan; those must never reach a fix.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


class GpsClient(threading.Thread):  # pylint: disable=too-many-instance-attributes
    """Background thread reading TPV/SKY reports from a gpsd daemon.

    Maintains the latest valid fix, available thread-safely via the ``fix``
    property. If gpsd is unreachable at startup, GPS support is disabled with
    a warning; if the connection is lost later it is retried with a backoff.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        stop_event: Optional[threading.Event] = None,
        reconnect_delay: float = RECONNECT_DELAY,
    ):
        """Initialize the gpsd client."""
        super().__init__(name="GpsClient", daemon=True)
        self.host = host
        self.port = int(port)
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.reconnect_delay = reconnect_delay
        self._lock = threading.Lock()
        self._fix: Optional[GpsFix] = None
        self._ever_connected = False
        self._report_counts: dict[str, int] = {}
        self._last_tpv: Optional[tuple] = None
        self._last_sky: Optional[tuple] = None
        self._peak_sky: Optional[tuple] = None

    @property
    def fix(self) -> Optional[GpsFix]:
        """Return the latest valid fix, or None if none is available."""
        with self._lock:
            return self._fix

    @property
    def enabled(self) -> bool:
        """Return True once a connection to gpsd has been established."""
        with self._lock:
            return self._ever_connected

    def _set_fix(self, fix: Optional[GpsFix]) -> None:
        """Store a fix, guarded by the lock, logging acquire/loss transitions."""
        with self._lock:
            previous = self._fix
            self._fix = fix
        if fix is not None and previous is None:
            alt = f", alt={fix.altitude:.1f}m" if fix.altitude is not None else ""
            logger.info(
                "GPS fix acquired: lat=%.6f, lon=%.6f%s",
                fix.lat,
                fix.lon,
                alt,
            )
        elif fix is None and previous is not None:
            logger.info("GPS fix lost")

    def _bump(self, key: str) -> None:
        """Increment a diagnostic counter, guarded by the lock."""
        with self._lock:
            self._report_counts[key] = self._report_counts.get(key, 0) + 1

    def status(self) -> str:
        """Return a human-readable snapshot for diagnosing lost fixes."""
        with self._lock:
            counts = dict(self._report_counts)
            last_tpv = self._last_tpv
            last_sky = self._last_sky
            peak_sky = self._peak_sky
            fix = self._fix
        if fix is None:
            fix_desc = "none"
        else:
            alt = f", alt={fix.altitude:.1f}m" if fix.altitude is not None else ""
            fix_desc = f"{fix.lat:.6f}, {fix.lon:.6f}{alt}"
        if last_tpv is None:
            tpv_desc = "no TPV seen"
        else:
            mode, status = last_tpv
            tpv_desc = f"mode={mode}, status={status}"
        if last_sky is None:
            sky_desc = "no SKY seen"
        else:
            sky_desc = (
                f"used-sats={last_sky[1]}, sats-in-view={last_sky[0]}, "
                f"best-snr={last_sky[2]:g}"
            )
        if peak_sky is None:
            peak_desc = "none"
        else:
            peak_desc = f"sats-in-view={peak_sky[0]}, best-snr={peak_sky[2]:g}"
        counts_desc = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        return (
            f"fix={fix_desc}; last TPV {tpv_desc}; last SKY {sky_desc}; "
            f"peak {peak_desc}; reports ({counts_desc or 'none'})"
        )

    def run(self) -> None:
        """Main loop: read from gpsd until stopped, reconnecting on failure."""
        while not self.stop_event.is_set():
            try:
                self._read_gpsd()
                return
            except (ConnectionError, OSError, ValueError) as e:
                if not self.enabled:
                    logger.warning(
                        "GPS disabled: gpsd not reachable at %s:%d (%s). "
                        "Install/start gpsd and ensure it owns the receiver.",
                        self.host,
                        self.port,
                        e,
                    )
                    return
                logger.warning(
                    "GPS: gpsd connection problem: %s; retrying in %.0fs",
                    e,
                    self.reconnect_delay,
                )
                # Never report a stale position while we cannot reach gpsd.
                self._set_fix(None)
                self.stop_event.wait(self.reconnect_delay)

    def _read_gpsd(self) -> None:
        """Connect to gpsd, subscribe, and stream JSON reports.

        Raises ConnectionError when the connection drops so run() can retry;
        returns normally only when stopped.
        """
        logger.info("GPS: connecting to gpsd at %s:%d", self.host, self.port)
        sock = socket.create_connection((self.host, self.port), timeout=READ_TIMEOUT)
        try:
            self._ever_connected = True
            logger.info("GPS: connected to gpsd at %s:%d", self.host, self.port)
            sock.settimeout(READ_TIMEOUT)
            sock.sendall(b'?WATCH={"enable":true,"json":true};\n')
            buffer = ""
            while not self.stop_event.is_set():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    raise ConnectionError("gpsd closed the connection")
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if line:
                        self._handle_line(line)
        finally:
            sock.close()

    def _handle_line(self, line: str) -> None:
        """Parse a JSON report line and update the fix when valid."""
        try:
            report = json.loads(line)
        except ValueError:
            logger.debug("GPS: ignoring non-JSON report %r", line)
            self._bump("bad_json")
            return
        if not isinstance(report, dict):
            return
        kind = report.get("class")
        if not isinstance(kind, str):
            kind = "??"
        self._bump(kind)
        logger.debug("GPS rx %s", line)
        if kind == "TPV":
            self._record_tpv_diag(report)
            self._handle_tpv(report)
        elif kind == "SKY":
            self._handle_sky(report)

    def _record_tpv_diag(self, report: dict) -> None:
        """Store the latest TPV mode/status for the status line."""
        with self._lock:
            self._last_tpv = (report.get("mode"), report.get("status"))

    def _handle_tpv(self, report: dict) -> None:
        """Update or clear the fix from a TPV report.

        Only satellite fixes are accepted: mode 2/3 (2D/3D) combined with a
        status that indicates a real fix (1/2/3/4/10). Dead reckoning (5),
        simulation (9) and no fix (0) clear any stored fix so we never report
        a stale or fabricated position.
        """
        mode = report.get("mode")
        accepted = (
            _tpv_status_accepted(report.get("status"))
            and isinstance(mode, int)
            and mode >= 2
        )
        lat = _finite(report.get("lat"))
        lon = _finite(report.get("lon"))
        if not accepted or lat is None or lon is None:
            logger.debug("GPS: no fix (mode=%r)", mode)
            self._set_fix(None)
            return
        altitude = _finite(report.get("alt"))
        self._set_fix(
            GpsFix(
                lat=lat,
                lon=lon,
                altitude=altitude,
                timestamp=_tpv_time(report.get("time")),
            )
        )

    def _handle_sky(self, report: dict) -> None:
        """Record satellites in view, used, and signal strength from a SKY.

        gpsd merges all constellations into a single SKY report, with signal
        strength in the ``ss`` field (dB-Hz) and a ``used`` flag per
        satellite. Stored as both the current snapshot and the all-time peak
        so status() can show live values as the antenna is moved.
        """
        satellites = report.get("satellites")
        if not isinstance(satellites, list):
            return
        sats_in_view = 0
        sats_used = 0
        best_snr = 0
        for satellite in satellites:
            if not isinstance(satellite, dict):
                continue
            if satellite.get("used"):
                sats_used += 1
            snr = _finite(satellite.get("ss"))
            if snr is not None:
                sats_in_view += 1
                best_snr = max(best_snr, snr)
        current = (sats_in_view, sats_used, best_snr)
        with self._lock:
            self._last_sky = current
            if self._peak_sky is None:
                self._peak_sky = current
            else:
                self._peak_sky = (
                    max(self._peak_sky[0], current[0]),
                    max(self._peak_sky[1], current[1]),
                    max(self._peak_sky[2], current[2]),
                )


def _resolve_gpsd(args) -> tuple[str, int]:
    """Resolve host and port from --config, overridden by CLI arguments.

    Falls back to 127.0.0.1:2947 when neither is specified.
    """
    host = DEFAULT_HOST
    port = DEFAULT_PORT
    if getattr(args, "config", None):
        with open(args.config, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        gps_data = data.get("gps") or {}
        host = gps_data.get("host", host)
        port = int(gps_data.get("port", port))
    if hasattr(args, "gpsd_host"):
        host = args.gpsd_host
    if hasattr(args, "gpsd_port"):
        port = args.gpsd_port
    return host, port


def main() -> int:
    """Wait for a GPS fix and print the current location without submitting.

    Reads the gps section of --config for the gpsd host and port, overridden
    by explicit --gpsd-host/--gpsd-port arguments. --verbose logs every JSON
    report plus a periodic status line (fix mode, satellites in view, signal
    strength) for diagnosing lost-fix issues. Exits 0 with the fix on success,
    1 if no fix arrives within --timeout, 2 on config errors, or the signal
    exit code if interrupted.
    """
    parser = argparse.ArgumentParser(
        description="Read and print the current GPS fix from gpsd."
    )
    parser.add_argument(
        "--config",
        default=None,
        help="path to config.yaml; the gps section provides host/port",
    )
    parser.add_argument(
        "--gpsd-host",
        default=argparse.SUPPRESS,
        help="gpsd address (overrides config; default 127.0.0.1)",
    )
    parser.add_argument(
        "--gpsd-port",
        type=int,
        default=argparse.SUPPRESS,
        help="gpsd TCP port (overrides config; default 2947)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="seconds to wait for a fix (default: 60)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="log every JSON report plus periodic diagnostic status",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="{asctime} - {levelname} - {message}",
        style="{",
    )

    try:
        host, port = _resolve_gpsd(args)
    except (OSError, yaml.YAMLError, ValueError) as e:
        logger.error("Failed to load GPS config: %s", e)
        return 2

    client = GpsClient(host=host, port=port)
    client.start()
    logger.info(
        "Waiting for GPS fix via gpsd on %s:%d (timeout %.0fs)",
        host,
        port,
        args.timeout,
    )

    try:
        deadline = time.time() + args.timeout
        next_status = time.time() + STATUS_INTERVAL
        while time.time() < deadline:
            fix = client.fix
            if fix is not None:
                return _print_fix(fix)
            if not client.is_alive() and not client.enabled:
                break
            if time.time() >= next_status:
                logger.info("GPS status: %s", client.status())
                next_status = time.time() + STATUS_INTERVAL
            time.sleep(0.5)
        print(f"No GPS fix within {args.timeout:.0f}s")
        return 1
    except KeyboardInterrupt:
        print("Interrupted")
        return 130
    finally:
        client.stop_event.set()
        client.join(timeout=5.0)


def _print_fix(fix: GpsFix) -> int:
    """Print a fix on a single line and return 0."""
    parts = [f"lat={fix.lat:.6f}", f"lon={fix.lon:.6f}"]
    if fix.altitude is not None:
        parts.append(f"alt={fix.altitude:.1f}")
    if fix.timestamp is not None:
        parts.append(f"ts={fix.timestamp.isoformat()}")
    print(f"GPS FIX {' '.join(parts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
