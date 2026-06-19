""" Read UAS Remote ID packets from a network interface or a PCAP file and
    relay them to a CalTopo map
"""

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import logging
import signal
import sqlite3
import sys
import threading
import time
from typing import Optional

import yaml
import requests
from requests.exceptions import RequestException
from scapy.layers.dot11 import Dot11
from scapy.all import rdpcap, sniff

from uas_remoteid.common.wifi import parse_dot11
from database import RemoteIDDatabase

logger = logging.getLogger(__name__)


@dataclass
class ApiClientConfig:
    """Configuration for an API client endpoint"""

    url: str
    api_key: str
    interval: int = 60  # seconds between checks
    batch_size: int = 200  # events per request

    def __init__(self, config_dict: dict):
        """Initialize from a configuration dictionary"""
        if "url" not in config_dict:
            raise ValueError("API client config missing required field: 'url'")
        if "api_key" not in config_dict:
            raise ValueError("API client config missing required field: 'api_key'")

        self.url = config_dict["url"].rstrip("/")
        self.api_key = config_dict["api_key"]
        self.interval = int(config_dict.get("interval", 60))
        self.batch_size = int(config_dict.get("batch_size", 200))


class ApiClientThread(threading.Thread):
    """Background thread that periodically sends data to a remote API server.

    This thread:
    - Queries the remote server for the last timestamp on startup
    - Periodically checks the local database for new records
    - Sends records in batches to the remote server
    - Retries immediately on any error (no backoff)
    """

    def __init__(
        self,
        config: ApiClientConfig,
        database: RemoteIDDatabase,
        stop_event: threading.Event,
    ):
        super().__init__(name=f"ApiClient-{config.url}", daemon=True)
        self.config = config
        self.database = database
        self.stop_event = stop_event
        self.last_timestamp: Optional[datetime] = None
        self.headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }
        self._running = True

    def _get_remote_last_timestamp(self) -> Optional[datetime]:
        """Query the remote server for the most recent timestamp"""
        try:
            url = f"{self.config.url}/api/last-timestamp"
            response = requests.get(url, headers=self.headers, timeout=30)
            response.raise_for_status()
            data = response.json()

            if data.get("last_timestamp"):
                ts = datetime.fromisoformat(data["last_timestamp"])
                logger.info(
                    "API client for %s: remote last timestamp is %s",
                    self.config.url,
                    ts,
                )
                return ts
            logger.info(
                "API client for %s: no data on remote server, starting from beginning",
                self.config.url,
            )
            return None
        except RequestException as e:
            logger.warning(
                "API client for %s: failed to get remote timestamp: %s. Will retry.",
                self.config.url,
                e,
            )
            return None

    def _send_batch(self, events: list[dict]) -> bool:
        """Send a batch of events to the remote server.

        Returns:
            True if successful, False otherwise
        """
        if not events:
            return True

        try:
            url = f"{self.config.url}/api/submit"
            response = requests.post(url, headers=self.headers, json=events, timeout=30)
            response.raise_for_status()
            result = response.json()

            if result.get("errors"):
                logger.warning(
                    "API client for %s: batch had %d validation errors",
                    self.config.url,
                    len(result["errors"]),
                )
                for error in result["errors"]:
                    logger.debug(
                        "  Index %d: %s", error.get("index"), error.get("reason")
                    )

            inserted = result.get("inserted", 0)
            logger.debug(
                "API client for %s: sent %d events, inserted %d",
                self.config.url,
                len(events),
                inserted,
            )

            # Update last timestamp from response if available
            if result.get("last_timestamp"):
                self.last_timestamp = datetime.fromisoformat(result["last_timestamp"])

            return True

        except RequestException as e:
            logger.warning(
                "API client for %s: failed to send batch: %s. Will retry.",
                self.config.url,
                e,
            )
            return False

    def _send_ping(self) -> bool:
        """Send a lightweight heartbeat to the remote server.

        Returns:
            True if successful, False otherwise
        """
        try:
            url = f"{self.config.url}/api/submit/ping"
            response = requests.get(url, headers=self.headers, timeout=30)
            response.raise_for_status()
            logger.debug("API client for %s: ping successful", self.config.url)
            return True
        except RequestException as e:
            logger.warning(
                "API client for %s: ping failed: %s", self.config.url, e
            )
            return False

    def _sync_once(self) -> None:
        """Perform one sync cycle: get pending events and send them"""
        if self.last_timestamp is None:
            # First run - check remote server for resume point
            self.last_timestamp = self._get_remote_last_timestamp()
            if self.last_timestamp is None:
                # No remote timestamp, check local database for minimum
                self.last_timestamp = datetime.min

        # Get pending events from database
        events = self.database.get_events_after(
            self.last_timestamp, limit=self.config.batch_size
        )

        if not events:
            logger.debug("API client for %s: no pending events", self.config.url)
            self._send_ping()
            return

        # Send the batch
        if self._send_batch(events):
            logger.info(
                "API client for %s: successfully sent %d events",
                self.config.url,
                len(events),
            )
        # If failed, we'll retry on next wake-up

    def run(self) -> None:
        """Main thread loop"""
        logger.info(
            "API client thread started for %s (interval=%ds, batch_size=%d)",
            self.config.url,
            self.config.interval,
            self.config.batch_size,
        )

        while not self.stop_event.is_set():
            try:
                self._sync_once()
            except (RequestException, sqlite3.Error, ValueError) as e:
                logger.error(
                    "API client for %s: error during sync: %s",
                    self.config.url,
                    e,
                )

            # Wait for next interval or until stopped
            self.stop_event.wait(self.config.interval)

        logger.info("API client thread stopped for %s", self.config.url)

    def stop(self) -> None:
        """Signal the thread to stop"""
        self._running = False
        self.stop_event.set()


@dataclass
class ServerConfig:  # pylint: disable=too-many-instance-attributes
    """Read configuration from a YAML file"""

    rate_limit: int
    ignore_list: set[str]
    allow_list: set[str]
    alias_map: dict[str, str]
    caltopo_url: str
    logging: str
    logging_level: int
    bpf_filter: str
    database: str = None

    def __init__(self, yaml_file: str):
        with open(yaml_file, encoding="utf-8") as fh:
            yaml_data = yaml.safe_load(fh)

        required_fields = ["caltopo_url"]
        for r_field in required_fields:
            if r_field not in yaml_data:
                raise ValueError(f"Missing required config field: {r_field}")

        self.logging = yaml_data["logging"]
        if self.logging == "INFO":
            self.logging_level = logging.INFO
        elif self.logging == "ERROR":
            self.logging_level = logging.ERROR
        elif self.logging == "DEBUG":
            self.logging_level = logging.DEBUG
        else:
            self.logging_level = logging.WARNING

        self.caltopo_url = yaml_data["caltopo_url"]
        self.rate_limit = int(yaml_data["rate_limit"])
        self.ignore_list = set(yaml_data.get("ignore", []))
        self.allow_list = set(yaml_data.get("allow", []))
        self.alias_map = yaml_data.get("alias", {})
        self.bpf_filter = yaml_data.get("filter", "type mgt")
        self.database = yaml_data.get("database")

        # Parse API client configurations
        self.api_clients: list[ApiClientConfig] = []
        api_clients_data = yaml_data.get("api_clients", [])
        for client_config in api_clients_data:
            try:
                self.api_clients.append(ApiClientConfig(client_config))
            except ValueError as e:
                logger.warning("Skipping invalid API client config: %s", e)

        if self.api_clients:
            logger.info(
                "Configured %d API client(s): %s",
                len(self.api_clients),
                ", ".join(c.url for c in self.api_clients),
            )


@dataclass
class UAS:  # pylint: disable=too-many-instance-attributes
    """Data received from a Remote ID packet"""

    id: str = None
    lat: str = None
    lon: str = None
    session_id: str = None
    mac_address: str = None
    altitude: float = None
    height: float = None
    height_type: int = None
    timestamp: datetime = field(default_factory=datetime.now)
    operator_id: str = None
    operator_lat: float = None
    operator_lon: float = None

    def valid(self) -> bool:
        """Check whether all fields have been populated. This is necessary
        because data is spread across multiple Remote ID packets, so won't
        all be received at the same time.
        Also ignore events where latitude or longitude is zero.
        """
        if self.id is None:
            return False
        if self.lat is None:
            return False
        if self.lon is None:
            return False
        if self.lat == 0:
            return False
        if self.lon == 0:
            return False
        return True

    def url_safe(self) -> bool:
        """Validate that id, lat, and lon are safe for use in a URL.
        - id: must be alphanumeric
        - lat, lon: must be strictly numeric (including negative sign and decimal)
        """
        if not self.id or not self.id.isalnum():
            logger.warning("Invalid UAS ID: %s", self.id)
            return False
        try:
            float(self.lat)
        except (TypeError, ValueError):
            logger.warning("Invalid latitude: %s", self.lat)
            return False
        try:
            float(self.lon)
        except (TypeError, ValueError):
            logger.warning("Invalid longitude: %s", self.lon)
            return False
        return True

    def get_altitude(self) -> float:
        """Get altitude as a float, returning 0 if None"""
        if self.altitude is None:
            return 0.0
        return float(self.altitude)


@dataclass
class Stats:
    """Statistics counters for packet processing"""

    received: int = 0
    discarded: int = 0
    rate_limited: int = 0
    recorded: int = 0
    reported: int = 0
    errors: int = 0
    unique_ids: set = None

    def __post_init__(self):
        if self.unique_ids is None:
            self.unique_ids = set()

    def reset(self):
        """Reset all counters to zero"""
        self.received = 0
        self.discarded = 0
        self.rate_limited = 0
        self.recorded = 0
        self.reported = 0
        self.errors = 0
        self.unique_ids.clear()


@dataclass
class Server:  # pylint: disable=too-many-instance-attributes
    """Handles UAS Remote ID packet processing and CalTopo reporting."""

    url_prefix: str
    last_update: dict[str, float]
    config: ServerConfig
    noop: bool = False
    database: RemoteIDDatabase = None
    stats: Stats = None
    stats_timer: threading.Timer = None
    api_client_threads: list[ApiClientThread] = None
    api_client_stop_event: threading.Event = None

    def report(self, uas):
        """Upload data to CalTopo and store in database"""

        current_time = time.time()
        last_update = self.last_update.get(uas.id, 0)
        delta = current_time - last_update
        if delta < self.config.rate_limit:
            logger.debug("Rate limited %s", uas.id)
            if self.stats:
                self.stats.rate_limited += 1
            return

        self.last_update[uas.id] = current_time

        # Store in database if configured
        if self.database:
            self.database.store(
                timestamp=uas.timestamp,
                mac_address=uas.mac_address or "",
                uas_id=uas.id,
                latitude=float(uas.lat),
                longitude=float(uas.lon),
                altitude=uas.get_altitude(),
                height=uas.height,
                height_type=uas.height_type,
                operator_id=uas.operator_id,
                operator_latitude=(
                    float(uas.operator_lat) if uas.operator_lat is not None else None
                ),
                operator_longitude=(
                    float(uas.operator_lon) if uas.operator_lon is not None else None
                ),
                session_id=uas.session_id,
            )
            if self.stats:
                self.stats.recorded += 1

        # Get alias for display/reporting, fallback to original ID
        display_id = self.config.alias_map.get(uas.id, uas.id)

        if self.noop:
            logger.info("TX %s %s %s (NOOP)", display_id, uas.lon, uas.lat)
            return
        logger.info("TX %s %s %s", display_id, uas.lon, uas.lat)

        url = f"{self.url_prefix}?id={display_id}&lat={uas.lat}&lng={uas.lon}"
        try:
            resp = requests.get(url, timeout=10)
            logger.debug("CalTopo %s %.100s", resp.status_code, resp.text)
            if self.stats:
                self.stats.reported += 1
        except RequestException as e:
            logger.error("Exception %s", e)
            if self.stats:
                self.stats.errors += 1

    def on_receive(self, packet):
        """Event handler for sniffed packets"""

        if not packet.haslayer(Dot11):
            return

        uas = self.decode_packet(packet)

        if not uas.valid():
            return

        if self.stats:
            self.stats.received += 1
            self.stats.unique_ids.add(uas.id)

        logger.debug("RX %s %s %s", uas.id, uas.lon, uas.lat)

        if uas.id in self.config.ignore_list:
            if self.stats:
                self.stats.discarded += 1
            return

        if self.config.allow_list and uas.id not in self.config.allow_list:
            if self.stats:
                self.stats.discarded += 1
            return

        if not uas.url_safe():
            if self.stats:
                self.stats.discarded += 1
            return

        self.report(uas)

    # NAN service ID for Remote ID (6 bytes = unique, no false positives)
    _NAN_SERVICE_ID = b"\x88\x69\x19\x9d\x92\x09"
    # Legacy OpenDroneID beacon signature (OUI fa:0b:bc followed by type 0x0d)
    _LEGACY_BEACON_SIG = b"\xfa\x0b\xbc\x0d"

    def _has_remoteid_signature(self, packet: Dot11) -> bool:
        """Fast check for Remote ID signatures in raw packet bytes.
        Checks for NAN service ID or Legacy beacon signature.
        Vendor-specific IEs are handled by the BPF filter.
        """
        raw = bytes(packet)
        if self._NAN_SERVICE_ID in raw:
            return True

        if self._LEGACY_BEACON_SIG in raw:
            return True

        return False

    def decode_packet(self, packet: Dot11) -> UAS:
        """Read the important bits from the Remote ID beacon and put them
        in a UAS object
        """

        uas = UAS()

        # Fast pre-filter: check raw bytes before expensive Scapy parsing
        if not self._has_remoteid_signature(packet):
            return uas

        # Cache getattr results to avoid repeated lookups
        addr2 = getattr(packet, "addr2", None)
        uas.mac_address = addr2

        # parse_dot11 yields only Remote ID messages - if empty, skip early
        msgs = list(parse_dot11(packet))
        if not msgs:
            return uas

        uas.mac_address = packet.addr2 if hasattr(packet, "addr2") else None
        for msg in msgs:
            for d in msg.data:
                msg_type = d.messageType
                if msg_type == 0:
                    uas.id = d.uasId.decode("utf-8")
                    if hasattr(d, "sessionId"):
                        uas.session_id = d.sessionId.decode("utf-8").rstrip("\x00")
                elif msg_type == 1:
                    uas.lat = d.latitude
                    uas.lon = d.longitude
                    # Try to get altitude - prefer geometric altitude if available
                    alt = getattr(d, "altitudeGeo", None)
                    if alt is None:
                        alt = getattr(d, "altitudeBaro", None)
                    uas.altitude = alt
                    uas.height = getattr(d, "height", None)
                    uas.height_type = getattr(d, "heightType", None)
                elif msg_type == 4:
                    uas.operator_lat = d.operatorLatitude
                    uas.operator_lon = d.operatorLongitude
                elif msg_type == 5:
                    uas.operator_id = d.operatorId.decode("utf-8").rstrip("\x00")
        return uas

    def print_stats(self):
        """Report statistics and restart timer"""
        if self.stats:
            logger.info(
                "Stats: received=%d, discarded=%d, rate_limited=%d, recorded=%d,"
                " reported=%d, errors=%d, unique_ids=%d",
                self.stats.received,
                self.stats.discarded,
                self.stats.rate_limited,
                self.stats.recorded,
                self.stats.reported,
                self.stats.errors,
                len(self.stats.unique_ids),
            )
            self.stats.reset()

    def _report_stats(self):
        """Report statistics and restart timer"""
        self.print_stats()
        self.stats_timer = threading.Timer(60.0, self._report_stats)
        self.stats_timer.daemon = True
        self.stats_timer.start()

    def start_stats_timer(self):
        """Start the statistics reporting timer"""
        self.stats = Stats()
        self._report_stats()

    def stop_stats_timer(self):
        """Stop the statistics reporting timer"""
        if self.stats_timer:
            self.stats_timer.cancel()

    def stop_api_clients(self):
        """Stop all API client threads"""
        if self.api_client_stop_event:
            logger.info("Stopping API client threads...")
            self.api_client_stop_event.set()
            for thread in self.api_client_threads:
                thread.join(timeout=5.0)
                if thread.is_alive():
                    logger.warning(
                        "API client thread %s did not stop gracefully", thread.name
                    )

    def __init__(self, config: ServerConfig, noop: bool = False):
        self.config = config
        self.url_prefix = self.config.caltopo_url
        self.last_update = {}
        self.noop = noop
        self.stats = None
        self.stats_timer = None
        logging.basicConfig(
            level=self.config.logging_level,
            format="{asctime} - {levelname} - {message}",
            style="{",
        )

        # Initialize database if configured
        if self.config.database:
            self.database = RemoteIDDatabase(self.config.database)

        # Start API client threads if configured and database is available
        self.api_client_threads = []
        self.api_client_stop_event = threading.Event()
        if self.config.api_clients and self.database:
            for client_config in self.config.api_clients:
                thread = ApiClientThread(
                    client_config, self.database, self.api_client_stop_event
                )
                thread.start()
                self.api_client_threads.append(thread)
        elif self.config.api_clients and not self.database:
            logger.warning(
                "API clients configured but no database enabled. "
                "Add 'database' to config to enable API client functionality."
            )


def signal_handler(signum, frame):  # pylint: disable=unused-argument
    """Catch system signals"""
    logger.info("Shutting down...")
    # Note: API clients will be stopped by the finally block in main
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    argparser = argparse.ArgumentParser()
    group = argparser.add_mutually_exclusive_group(required=True)
    _ = group.add_argument("--pcap", help="pcap file to read packets from")
    _ = group.add_argument(
        "--interface", help="name of wireless interface to sniff from"
    )
    _ = argparser.add_argument(
        "--config", default="config.yaml", help="yaml configuration file"
    )
    _ = argparser.add_argument(
        "--noop",
        action="store_true",
        help="do not send data to CalTopo (no operation mode)",
    )
    args = argparser.parse_args()

    conf = ServerConfig(args.config)
    serv = Server(conf, noop=args.noop)

    if args.pcap:
        serv.stats = Stats()
        for p in rdpcap(args.pcap):
            serv.on_receive(p)
        serv.print_stats()

    elif args.interface:
        serv.start_stats_timer()
        try:
            logger.info("Listening for packets %s", args.interface)
            while True:
                sniff(
                    iface=args.interface,
                    filter=conf.bpf_filter,
                    prn=serv.on_receive,
                    store=0,
                )
        except KeyboardInterrupt:
            pass
        finally:
            serv.stop_stats_timer()
            serv.stop_api_clients()
