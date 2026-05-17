""" Read UAS Remote ID packets from a network interface or a PCAP file and
    relay them to a CalTopo map
"""

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import time
import logging
import signal
import sys

import yaml
import requests
from requests.exceptions import RequestException
from scapy.layers.dot11 import Dot11
from scapy.all import rdpcap, sniff

from uas_remoteid.common.wifi import parse_dot11
from database import RemoteIDDatabase

logger = logging.getLogger(__name__)


@dataclass
class ServerConfig:
    """Read configuration from a YAML file"""

    rate_limit: int
    ignore_list: set[str]
    allow_list: set[str]
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
        self.bpf_filter = yaml_data.get("filter", "type mgt")
        self.database = yaml_data.get("database")


@dataclass
class UAS:  # pylint: disable=too-many-instance-attributes
    """Data received from a Remote ID packet"""

    id: str = None
    lat: str = None
    lon: str = None
    session_id: str = None
    mac_address: str = None
    altitude: float = None
    timestamp: datetime = field(default_factory=datetime.now)
    operator_id: str = None
    operator_lat: float = None
    operator_lon: float = None

    def valid(self) -> bool:
        """Check whether all fields have been populated. This is necessary
        because data is spread across multiple Remote ID packets, so won't
        all be received at the same time.
        """
        if self.id is None:
            return False
        if self.lat is None:
            return False
        if self.lon is None:
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
class Server:
    """Handles UAS Remote ID packet processing and CalTopo reporting."""

    url_prefix: str
    last_update: dict[str, float]
    config: ServerConfig
    noop: bool = False
    database: RemoteIDDatabase = None

    def report(self, uas):
        """Upload data to CalTopo and store in database"""

        current_time = time.time()
        last_update = self.last_update.get(uas.id, 0)
        delta = current_time - last_update
        if delta < self.config.rate_limit:
            logger.debug("Rate limited %s", uas.id)
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
                operator_id=uas.operator_id,
                operator_latitude=(
                    float(uas.operator_lat) if uas.operator_lat is not None else None
                ),
                operator_longitude=(
                    float(uas.operator_lon) if uas.operator_lon is not None else None
                ),
                session_id=uas.session_id,
            )

        if self.noop:
            logger.info("TX %s %s %s (NOOP)", uas.id, uas.lon, uas.lat)
            return
        logger.info("TX %s %s %s", uas.id, uas.lon, uas.lat)

        url = f"{self.url_prefix}?id={uas.id}&lat={uas.lat}&lng={uas.lon}"
        try:
            resp = requests.get(url, timeout=10)
            logger.debug("CalTopo %s %.100s", resp.status_code, resp.text)
        except RequestException as e:
            logger.error("Exception %s", e)

    def on_receive(self, packet):
        """Event handler for sniffed packets"""

        if not packet.haslayer(Dot11):
            return

        uas = self.decode_packet(packet)

        if not uas.valid():
            return

        logger.debug("RX %s %s %s", uas.id, uas.lon, uas.lat)

        if uas.id in self.config.ignore_list:
            return

        if self.config.allow_list and uas.id not in self.config.allow_list:
            return

        if not uas.url_safe():
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
                elif msg_type == 4:
                    uas.operator_lat = d.operatorLatitude
                    uas.operator_lon = d.operatorLongitude
                elif msg_type == 5:
                    uas.operator_id = d.operatorId.decode("utf-8").rstrip("\x00")
        return uas

    def __init__(self, config: ServerConfig, noop: bool = False):
        self.config = config
        self.url_prefix = self.config.caltopo_url
        self.last_update = {}
        self.noop = noop
        logging.basicConfig(
            level=self.config.logging_level,
            format="{asctime} - {levelname} - {message}",
            style="{",
        )

        # Initialize database if configured
        if self.config.database:
            self.database = RemoteIDDatabase(self.config.database)


def signal_handler(signum, frame):  # pylint: disable=unused-argument
    """Catch system signals"""
    logger.info("Shutting down...")
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
        for p in rdpcap(args.pcap):
            serv.on_receive(p)

    elif args.interface:
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
