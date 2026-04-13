# UAS Remote ID to CalTopo

An application to receive WiFi Remote ID broadcasts via a WiFi interface in monitor mode, collect the UAS ID, latitude, and longitude, and relay them to a map in [CalTopo](https://caltopo.com/).

This was developed to support drone tracking for search and rescue operations, but can be used for any application requiring real-time drone position monitoring.

## Overview

Remote ID is a broadcast system that transmits identification and location information from UASes (aka drones). This tool captures those broadcasts and forwards the position data to CalTopo, enabling real-time tracking of multiple drones on a shared map.

## Features

- **Live Packet Capture**: Capture Remote ID broadcasts from a WiFi interface in monitor mode
- **PCAP Replay**: Replay and analyze previously captured Remote ID packets from PCAP files
- **CalTopo Integration**: Send position updates directly to CalTopo maps
- **Ignore List**: Filter out specific UAS IDs from reporting
- **Rate Limiting**: Configurable throttling to prevent API spam

## Dependencies

This project heavily depends on [scapy-uas-remoteid](https://github.com/aeburriel/scapy-uas-remoteid/) for decoding Remote ID packets.

### Requirements

- Python 3.8+
- Linux system with WiFi interface supporting monitor mode
- Root/sudo privileges (required for monitor mode and packet capture)

### Python Packages

```
scapy==2.5.0
pyyaml
requests
```

## Installation

1. Clone the repository:
```bash
git clone https://github.com/heytensai/remoteid-caltopo-tracker
cd remoteid-caltopo-tracker
```

2. Install Python dependencies:
```bash
pip install -r requirements.txt
```

3. Copy the default configuration:
```bash
cp default_config.yaml config.yaml
```

4. Edit `config.yaml` with your CalTopo URL and preferences (see [Configuration](#configuration) below).

## Configuration

Edit `config.yaml` to configure the application:

```yaml
# Minimum seconds between updates for the same drone (prevents API spam)
rate_limit: 5

# Logging level: INFO or WARNING
logging: 'INFO'

# Your CalTopo position report URL
caltopo_url: 'https://caltopo.com/api/v1/position/report/<your_key_here>'

# List of UAS IDs to ignore (e.g., your own drones)
ignore:
  - "<your_uas_id_here>"
```

### Getting a CalTopo URL

1. Create or open a map in CalTopo
2. Go to **Config** → **URL Sharing**
3. Enable sharing and copy the position report URL
4. Paste this URL into your `config.yaml`

### Rate Limiting

Remote ID broadcasts can arrive multiple times per second, often with identical location data. The `rate_limit` setting prevents spamming the CalTopo API by only sending updates at the specified interval (in seconds). **It is strongly recommended to keep this enabled.**

## Usage

### Enable WiFi Monitor Mode

Your WiFi interface must be in monitor mode to intercept 802.11 management beacons:

```bash
# Replace 'wifi0' with your actual interface name (e.g., wlan0, wlp2s0)
sudo ip link set dev wifi0 down
sudo iwconfig wifi0 mode monitor
sudo ip link set dev wifi0 up

# Set the channel (commonly channel 6 for Remote ID)
sudo iw dev wifi0 set channel 6
```

To find your WiFi interface name:
```bash
ip link show
```

### Live Capture

Capture Remote ID broadcasts in real-time:

```bash
sudo python decoder.py --interface wifi0 --config config.yaml
```

### Replay from PCAP

Replay previously captured packets:

```bash
python decoder.py --pcap capture.pcap --config config.yaml
```

### Command-Line Options

```
usage: decoder.py [-h] (--pcap PCAP | --interface INTERFACE) --config CONFIG

options:
  -h, --help            show this help message and exit
  --pcap PCAP           Path to PCAP file for replay
  --interface INTERFACE WiFi interface name (must be in monitor mode)
  --config CONFIG       Path to configuration YAML file
```

## Project Structure

```
.
├── decoder.py           # Main application script
├── config.yaml          # Your configuration (created from default)
├── default_config.yaml  # Example configuration template
├── requirements.txt     # Python dependencies
├── uas_remoteid/        # Remote ID packet decoder library
│   ├── common/
│   ├── dji/
│   ├── opendroneid/
│   └── sgdsn/
└── README.md           # This file
```

## Logging

The application outputs log messages in the following format:

- `RX <id> <lon> <lat>` - Received a valid Remote ID packet
- `TX <id> <lon> <lat>` - Sent position update to CalTopo
- `RL <id>` - Rate limited (update skipped)
- `ER <error>` - Error occurred during transmission

## Troubleshooting

### No packets being received

- Verify your WiFi interface is in monitor mode: `iwconfig`
- Check you're on the correct channel (try channels 1, 6, or 149)
- Ensure you have sufficient privileges (run with `sudo`)
- Verify drones are actually broadcasting Remote ID nearby

### CalTopo not updating

- Check your CalTopo URL is correct in `config.yaml`
- Verify the URL is a position report URL, not a map sharing URL
- Check for `ER` messages in the logs indicating connection errors
- Ensure `rate_limit` isn't set too high

### Permission denied errors

Packet capture requires root privileges. Always run with `sudo` for live capture.

## Security Considerations

- Keep your CalTopo URL private - anyone with the URL can report positions to your map
- The ignore list helps filter out your own drones from being tracked

## License

See [LICENSE](LICENSE) for details.

## Acknowledgments

- [scapy-uas-remoteid](https://github.com/aeburriel/scapy-uas-remoteid/) - Remote ID packet decoding library
- [CalTopo](https://caltopo.com/) - Mapping platform for search and rescue

## Other projects of a similar ilk

These are some other RemoteID projects which I was unable to use for various reasons but which might be relevant for some.

- https://github.com/iannil/open-remote-id-parser/
- https://github.com/dronetag/python-odid/
