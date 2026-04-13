# About

An application to receive wifi Remote ID broadcasts via a wifi interface in monitor
mode, collect the UAS ID, latitude, and longitude and relay them to a map in CalTopo.

This was developed to support drone tracking for search and rescue purposes, but of
course could be used for any number of other useful tasks.

# Dependencies

Heavily depends on scap-uas-remoteid. I looked at a lot of options for collecting
the Remote ID packets, including writing my own decoder, and this was far and away
the best one.

- https://github.com/aeburriel/scapy-uas-remoteid/

# Features

- Live packet capture from an interface in monitor mode
- Replay from PCAP files
- Configurable CalTopo endpoint
- Ignore list for UAS IDs
- Rate limit of CalTopo updates

# Rate limiting

In order to avoid spamming the CalTopo APIs, location updates will be throttled as
per the configuration. I would not disable this as Remote ID broadcasts can often
come in many packets per second, with the same exact location data even. I haven't
really looked into why just that it does.

# Enable wifi monitor mode

Your wifi interface needs to be in monitor mode so we can intercept 802.11
management beacons.

- ip link set dev wifi0 down
- iwconfig wifi0 mode monitor
- ip link set dev wifi0 up

Make sure you set the correct channel, usually 6.

- iw dev wifi0 set channel 6
