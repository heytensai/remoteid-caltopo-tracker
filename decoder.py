import yaml
import time
import logging
import requests
from uas_remoteid.common.wifi import parse_dot11
from scapy.layers.dot11 import Dot11
from scapy.all import rdpcap, sniff
import sys
from pprint import pprint
from dataclasses import dataclass
import argparse

logger = logging.getLogger(__name__)

@dataclass
class server_config:
    rate_limit: int
    ignore_list: set[str]
    caltopo_url: str
    logging: str
    logging_level: int

    def __init__(self, yaml_file: str):
        with open(yaml_file, "r") as fh:
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
    id: str = None
    lat: str = None
    lon: str = None

    def valid(self) -> bool:
        if self.id is None:
            return False
        if self.lat is None:
            return False
        if self.lat is None:
            return False
        return True

@dataclass
class server:
    url_prefix: str
    last_update: int
    config: server_config

    def report(self, uas):
        current_time = time.time()
        delta = current_time - self.last_update
        if delta < self.config.rate_limit:
            logger.info(f"RL {uas.id}")
            return

        self.last_update = current_time
        logger.info(f"TX {uas.id} {uas.lon} {uas.lat}")
        url = f"{self.url_prefix}?id={uas.id}&lat={uas.lat}&lng={uas.lon}"
        try:
            resp = requests.get(url)
        except Exception as e:
            logger.error(f"ER {e}")
            pass

    def on_receive(self, packet):
        if not packet.haslayer(Dot11):
            return
        dot11 = packet[Dot11]

        uas = self.decode_packet(packet)

        if not uas.valid():
            return

        logger.info(f"RX {uas.id} {uas.lon} {uas.lat}")

        if uas.id in self.config.ignore_list:
            return

        self.report(uas)

    def decode_packet(self, packet: Dot11) -> UAS:
        uas = UAS()
        for msg in parse_dot11(packet):
            #pprint(msg)
            #print(msg.messageType)
            for d in msg.data:
                #print(d.messageType)
                if d.messageType == 0:
                    uas.id = d.uasId.decode("utf-8") 
                if d.messageType == 1:
                    uas.lat = d.latitude
                    uas.lon = d.longitude
        return uas

    def __init__(self, yaml_file: str):
        self.config = server_config(yaml_file)
        self.url_prefix = self.config.caltopo_url
        self.last_update = time.time() - self.config.rate_limit
        logging.basicConfig(level=self.config.logging_level, format="{asctime} - {levelname} - {message}", style="{")

def sniffer_packet(packet):
    if not packet.haslayer(Dot11):
        return
    dot11 = packet[Dot11]

    uas = decode_packet(dot11)
    if uas.valid():
        print(f"{uas.id} {uas.lat} {uas.lon}")

if __name__ == "__main__":
    argparser = argparse.ArgumentParser()
    group = argparser.add_mutually_exclusive_group(required=True)
    _ = group.add_argument("--pcap", help="")
    _ = group.add_argument("--interface", help="")
    _ = argparser.add_argument("--config", required=True, help="")
    args = argparser.parse_args()

    serv = server(args.config)

    if args.pcap:
        for packet in rdpcap(args.pcap):
            serv.on_receive(packet)

    elif args.interface:
        try:
            sniff(iface=args.interface, prn=serv.on_receive, store=0)
        except KeyboardInterrupt:
            pass
