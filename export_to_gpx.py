#!/usr/bin/env python3
"""Standalone script to export Remote ID data from SQLite to GPX track.

Supports filtering by date and/or UAS ID.
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom


def prettify_xml(elem: Element) -> str:
    """Return a pretty-printed XML string for the Element.
    """
    rough_string = tostring(elem, encoding='unicode')
    reparsed = minidom.parseString(rough_string)
    return reparsed.toprettyxml(indent="  ")


def add_uas_points(uas_id, points, gpx): # pylint: disable=too-many-locals
    """write GPX details for this UAS's points
    """
    trk = SubElement(gpx, 'trk')
    trk_name = SubElement(trk, 'name')
    trk_name.text = uas_id

    trkseg = SubElement(trk, 'trkseg')

    if len(points) > 0:
        # Start waypoint
        start_pt = points[0]
        wpt_start = SubElement(gpx, 'wpt')
        wpt_start.set('lat', str(start_pt['latitude']))
        wpt_start.set('lon', str(start_pt['longitude']))
        if start_pt['altitude'] is not None:
            ele_start = SubElement(wpt_start, 'ele')
            ele_start.text = str(start_pt['altitude'])
        name_start = SubElement(wpt_start, 'name')
        name_start.text = f"{uas_id} Start"

        # Operator waypoint
        if start_pt['operator_latitude'] != 0 and start_pt['operator_longitude'] != 0:
            oper_start = SubElement(gpx, 'wpt')
            oper_start.set('lat', str(start_pt['operator_latitude']))
            oper_start.set('lon', str(start_pt['operator_longitude']))
            oper_name_start = SubElement(oper_start, 'name')
            oper_name_start.text = f"{uas_id} Operator"

    if len(points) > 1:
        # End waypoint
        end_pt = points[-1]
        wpt_end = SubElement(gpx, 'wpt')
        wpt_end.set('lat', str(end_pt['latitude']))
        wpt_end.set('lon', str(end_pt['longitude']))
        if end_pt['altitude'] is not None:
            ele_end = SubElement(wpt_end, 'ele')
            ele_end.text = str(end_pt['altitude'])
        name_end = SubElement(wpt_end, 'name')
        name_end.text = f"{uas_id} End"

        # track that connects all the points
        # we can only create a track if at least 2 points exist
        for point in points:
            trkpt = SubElement(trkseg, 'trkpt')
            trkpt.set('lat', str(point['latitude']))
            trkpt.set('lon', str(point['longitude']))

            if point['altitude'] is not None:
                ele = SubElement(trkpt, 'ele')
                ele.text = str(point['altitude'])

            time_elem = SubElement(trkpt, 'time')
            # Format timestamp as ISO 8601
            ts = point['timestamp']
            if isinstance(ts, str):
                time_elem.text = ts
            else:
                time_elem.text = ts.isoformat()


def create_gpx_track(points_by_uas: dict[str, list[dict]]) -> Element:
    """Create a GPX Element from a dictionary of track points grouped by UAS ID.

    Args:
        points_by_uas: Dictionary mapping UAS ID to list of track points.
                      Each point should have: timestamp, latitude, longitude, altitude, uas_id
    """
    # GPX root element with namespaces
    gpx = Element('gpx')
    gpx.set('version', '1.1')
    gpx.set('creator', 'RemoteID-to-GPX')
    gpx.set('xmlns', 'http://www.topografix.com/GPX/1/1')
    gpx.set('xmlns:xsi', 'http://www.w3.org/2001/XMLSchema-instance')
    gpx.set('xsi:schemaLocation',
        'http://www.topografix.com/GPX/1/1 http://www.topografix.com/GPX/1/1/gpx.xsd')

    # Metadata
    metadata = SubElement(gpx, 'metadata')
    name = SubElement(metadata, 'name')
    name.text = "Remote ID Tracks"
    time = SubElement(metadata, 'time')
    time.text = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    # Create one track per UAS ID
    for uas_id, points in points_by_uas.items():
        add_uas_points(uas_id, points, gpx)

    return gpx


def query_database(db_path: str, uas_id: str = None, start_date: str = None,
                   end_date: str = None) -> list[dict]:
    """Query the SQLite database for Remote ID records.

    Args:
        db_path: Path to the SQLite database
        uas_id: Filter by UAS ID (optional)
        start_date: Filter records from this date (inclusive, ISO format, optional)
        end_date: Filter records until this date (inclusive, ISO format, optional)

    Returns:
        List of dictionaries containing track point data
    """
    if not Path(db_path).exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    query = "SELECT * FROM remoteid WHERE 1=1"
    params = []

    if uas_id:
        query += " AND uas_id = ?"
        params.append(uas_id)

    if start_date:
        query += " AND timestamp >= ?"
        params.append(start_date)

    if end_date:
        query += " AND timestamp <= ?"
        params.append(end_date)

    query += " ORDER BY timestamp"

    cursor = conn.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    points = []
    for row in rows:
        points.append({
            'timestamp': row['timestamp'],
            'latitude': row['latitude'],
            'longitude': row['longitude'],
            'altitude': row['altitude'],
            'uas_id': row['uas_id'],
            'mac_address': row['mac_address'],
            'operator_id': row['operator_id'],
            'operator_latitude': row['operator_latitude'],
            'operator_longitude': row['operator_longitude'],
        })

    return points


def parse_date(date_str: str) -> str:
    """Parse and validate a date string.

    Accepts ISO format dates (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)
    """
    try:
        # Try parsing as full datetime
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        return dt.isoformat()
    except ValueError:
        pass

    try:
        # Try parsing as date only
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        return dt.isoformat()
    except ValueError:
        pass

    raise ValueError(f"Invalid date format: {date_str}. Use YYYY-MM-DD or ISO datetime.")

def do_query(args, start_date: datetime, end_date: datetime):
    """Query the database and output as a gpx
    """
    try:
        print(f"Querying database: {args.database}")
        points = query_database(args.database, args.uas_id, start_date, end_date)
        print(f"Found {len(points)} records")

        if not points:
            print("No records found matching the criteria.")
            sys.exit(0)

        # Group points by UAS ID
        points_by_uas: dict[str, list[dict]] = {}
        for point in points:
            uas_id = point['uas_id'] or 'Unknown'
            if uas_id not in points_by_uas:
                points_by_uas[uas_id] = []
            points_by_uas[uas_id].append(point)

        print(f"Found {len(points_by_uas)} unique UAS ID(s): {', '.join(points_by_uas.keys())}")
        return points_by_uas
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        sys.exit(1)

def write_gpx(points, output_file):
    """write gps points to a gpx file
    """

    try:
        # Create GPX with one track per UAS ID
        gpx = create_gpx_track(points)

        # Write output
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        xml_content = prettify_xml(gpx)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(xml_content)

        print(f"GPX file written to: {output_path.absolute()}")

    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

def main():
    """it's main, what do you want from me?
    """

    parser = argparse.ArgumentParser(
        description='Export Remote ID data from SQLite to GPX track file.'
    )
    parser.add_argument('database', help='Path to SQLite database file')
    parser.add_argument('-o', '--output', help='Output GPX file path', required=True)
    parser.add_argument('--uas-id', help='Filter by UAS ID')
    parser.add_argument('--start-date',
        help='Filter records from this date (YYYY-MM-DD or ISO datetime)')
    parser.add_argument('--end-date',
        help='Filter records until this date (YYYY-MM-DD or ISO datetime)')
    parser.add_argument('--date', help='Filter records for a specific date (YYYY-MM-DD)')

    args = parser.parse_args()

    # Validate arguments
    if not args.uas_id and not args.start_date and not args.end_date and not args.date:
        parser.error("At least one filter is required: --uas-id, --date, --start-date, --end-date")

    # Handle --date shorthand (sets both start and end to that day)
    start_date = args.start_date
    end_date = args.end_date
    if args.date:
        try:
            dt = datetime.strptime(args.date, '%Y-%m-%d')
            start_date = dt.strftime('%Y-%m-%d 00:00:00')
            end_date = dt.strftime('%Y-%m-%d 23:59:59')
        except ValueError:
            parser.error("Invalid date format for --date. Use YYYY-MM-DD.")

    # Validate and parse dates
    try:
        if start_date:
            start_date = parse_date(start_date)
        if end_date:
            end_date = parse_date(end_date)
    except ValueError as e:
        parser.error(str(e))

    points = do_query(args, start_date, end_date)
    write_gpx(points, args.output)

if __name__ == '__main__':
    main()
