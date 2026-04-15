""" Read UAS Remote ID packets from a network interface or a PCAP file and
    relay them to a CalTopo map
"""

import argparse
from dataclasses import dataclass
import time
import logging

import yaml
import requests
from requests.exceptions import RequestException
from scapy.layers.dot11 import Dot11
from scapy.all import rdpcap, sniff

from uas_remoteid.common.wifi import parse_dot11

logger = logging.getLogger(__name__)

@dataclass
class ServerConfig:
    """ Read configuration from a YAML file
    """

    rate_limit: int
    ignore_list: set[str]
    caltopo_url: str
    logging: str
    logging_level: int

    def __init__(self, yaml_file: str):
        with open(yaml_file, encoding="utf-8") as fh:
            yaml_data = yaml.safe_load(fh)

        self.logging = yaml_data["logging"]
        if self.logging == "INFO":
            self.logging_level = logging.INFO
        else:
            self.logging_level = logging.WARNING

        self.caltopo_url = yaml_data["caltopo_url"]
        self.rate_limit = int(yaml_data["rate_limit"])
        self.ignore_list = set(yaml_data.get("ignore", []))

@dataclass
class UAS:
    """ Data received from a Remote ID packet
    """

    id: str = None
    lat: str = None
    lon: str = None

    def valid(self) -> bool:
        """ Check whether all fields have been populated. This is necessary
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
        """ Validate that id, lat, and lon are safe for use in a URL.
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

@dataclass
class Server:
    """ Handles UAS Remote ID packet processing and CalTopo reporting.
    """

    url_prefix: str
    last_update: int
    config: ServerConfig
    noop: bool = False

    def report(self, uas):
        """ Upload data to CalTopo
        """

        current_time = time.time()
        delta = current_time - self.last_update
        if delta < self.config.rate_limit:
            logger.info("RL %s", uas.id)
            return

        self.last_update = current_time

        if self.noop:
            logger.info("TX %s %s %s (NOOP)", uas.id, uas.lon, uas.lat)
            return
        logger.info("TX %s %s %s", uas.id, uas.lon, uas.lat)

        url = f"{self.url_prefix}?id={uas.id}&lat={uas.lat}&lng={uas.lon}"
        try:
            resp = requests.get(url, timeout=10)
            logger.debug("RP %s %.100s", resp.status_code, resp.text)
        except RequestException as e:
            logger.error("NT: %s", e)

    def on_receive(self, packet):
        """ Event handler for sniffed packets
        """

        if not packet.haslayer(Dot11):
            return

        uas = self.decode_packet(packet)

        if not uas.valid():
            return

        logger.info("RX %s %s %s", uas.id, uas.lon, uas.lat)

        if uas.id in self.config.ignore_list:
            return

        if not uas.url_safe():
            return

        self.report(uas)

    def decode_packet(self, packet: Dot11) -> UAS:
        """ Read the important bits from the Remote ID beacon and put them
            in a UAS object
        """

        uas = UAS()
        for msg in parse_dot11(packet):
            for d in msg.data:
                if d.messageType == 0:
                    uas.id = d.uasId.decode("utf-8")
                if d.messageType == 1:
                    uas.lat = d.latitude
                    uas.lon = d.longitude
        return uas

    def __init__(self, yaml_file: str, noop: bool = False):
        self.config = ServerConfig(yaml_file)
        self.url_prefix = self.config.caltopo_url
        self.last_update = time.time() - self.config.rate_limit
        self.noop = noop
        logging.basicConfig(level=self.config.logging_level,
            format="{asctime} - {levelname} - {message}", style="{")

if __name__ == "__main__":
    argparser = argparse.ArgumentParser()
    group = argparser.add_mutually_exclusive_group(required=True)
    _ = group.add_argument("--pcap", help="pcap file to read packets from")
    _ = group.add_argument("--interface", help="name of wireless interface to sniff from")
    _ = argparser.add_argument("--config", required=True, help="yaml configuration file")
    _ = argparser.add_argument("--noop", action="store_true", help="do not send data to CalTopo (no operation mode)")
    args = argparser.parse_args()

    serv = Server(args.config, noop=args.noop)

    if args.pcap:
        for p in rdpcap(args.pcap):
            serv.on_receive(p)

    elif args.interface:
        try:
            sniff(iface=args.interface, prn=serv.on_receive, store=0)
        except KeyboardInterrupt:
            pass
