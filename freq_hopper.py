"""Channel hopper for WiFi interface in monitor mode."""

import argparse
import subprocess
import time

DEFAULT_CHANNELS_2 = [1, 6, 11]
DEFAULT_CHANNELS_5 = [36, 40, 44, 48, 149, 153, 157, 161]
DEFAULT_DWELL_TIME = 0.1


def set_channel(interface, channel):
    """Set the channel on the given WiFi interface."""
    print(f"set channel {interface} {channel}")
    subprocess.run(
        ["sudo", "iw", "dev", interface, "set", "channel", str(channel)],
        capture_output=True,
        check=True,
    )


def parse_channels(channel_str):
    """Parse comma-separated channel numbers into a list of integers."""
    if not channel_str:
        return []
    return [int(ch.strip()) for ch in channel_str.split(",")]


def main():
    """Main entry point for the channel hopper."""
    parser = argparse.ArgumentParser(
        description="Channel hopper for WiFi interface in monitor mode."
    )
    parser.add_argument(
        "--interface",
        "-i",
        required=True,
        help="WiFi interface name (must be in monitor mode)",
    )
    parser.add_argument(
        "--sleep",
        "-s",
        type=float,
        default=DEFAULT_DWELL_TIME,
        help="Sleep time in seconds between channel switches",
    )
    parser.add_argument(
        "--channels-2",
        "-2",
        type=parse_channels,
        metavar="CH1,CH2,...",
        default=DEFAULT_CHANNELS_2,
        help=("Comma-separated list of 2.4GHz channels "
              f'(default: {",".join(map(str, DEFAULT_CHANNELS_2))})'),
    )
    parser.add_argument(
        "--channels-5",
        "-5",
        type=parse_channels,
        metavar="CH1,CH2,...",
        default=DEFAULT_CHANNELS_5,
        help=("Comma-separated list of 5GHz channels "
              f'(default: {",".join(map(str, DEFAULT_CHANNELS_5))})'),
    )
    args = parser.parse_args()

    channels = args.channels_2 + args.channels_5

    if not channels:
        parser.error(
            "At least one channel must be specified via --channels-2 or --channels-5"
        )

    try:
        while True:
            for ch in channels:
                set_channel(args.interface, ch)
                time.sleep(args.sleep)
    except KeyboardInterrupt:
        print("\nShutting down...")


if __name__ == "__main__":
    main()
