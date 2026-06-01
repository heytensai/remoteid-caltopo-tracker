"""Flask web interface for Remote ID visualization"""

import argparse
import logging
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, render_template
from flask_cors import CORS

from config import WebConfig
from database import WebDatabase
from sync import create_sync_manager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global instances
config: WebConfig = None
database: WebDatabase = None
sync_manager = None

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes


@app.route('/')
def index():
    """Main page"""
    return render_template('index.html')


@app.route('/api/config')
def get_config():
    """Get map configuration"""
    return jsonify({
        'map': {
            'center_lat': config.map.center_lat,
            'center_lon': config.map.center_lon,
            'default_zoom': config.map.default_zoom,
            'tile_provider': config.map.tile_provider
        },
        'default_hours': config.default_hours,
        'sync_enabled': sync_manager is not None
    })


@app.route('/api/sync/status', methods=['GET'])
def get_sync_status():
    """Get sync thread status"""
    if sync_manager:
        return jsonify({'enabled': True})
    else:
        return jsonify({'enabled': False})


@app.route('/api/sync/status', methods=['POST'])
def set_sync_status():
    """Enable or disable sync thread"""
    global sync_manager
    data = request.get_json()
    enabled = data.get('enabled', True)

    if sync_manager:
        if enabled:
            sync_manager.start()
        else:
            sync_manager.stop()
        return jsonify({'status': 'ok', 'enabled': enabled})
    else:
        return jsonify({'status': 'disabled', 'enabled': False}), 400


@app.route('/api/sync/collectors')
def get_collectors_status():
    """Get status of all sync collectors"""
    if sync_manager:
        collectors_status = []
        for collector in sync_manager.collectors:
            last_sync = sync_manager._last_sync.get(collector.name)
            collectors_status.append({
                'name': collector.name,
                'host': collector.host,
                'path': collector.remote_db_path,
                'last_sync': last_sync.strftime('%Y-%m-%d %H:%M') if last_sync else 'Never'
            })
        return jsonify({'collectors': collectors_status})
    else:
        return jsonify({'collectors': []})


@app.route('/api/drones')
def get_drones():
    """Get list of unique drones in time window"""
    try:
        start, end = _parse_time_range(request.args)
        drones = database.get_drones(start, end)
        return jsonify({'drones': drones})
    except Exception as e:
        logger.error("Error getting drones: %s", e)
        return jsonify({'error': str(e)}), 500


@app.route('/api/positions')
def get_positions():
    """Get positions in time window"""
    try:
        start, end = _parse_time_range(request.args)
        uas_id = request.args.get('uas_id')
        limit = min(int(request.args.get('limit', config.max_positions_per_query)),
                   config.max_positions_per_query)

        positions = database.get_positions(start, end, uas_id, limit)
        return jsonify({'positions': positions})
    except Exception as e:
        logger.error("Error getting positions: %s", e)
        return jsonify({'error': str(e)}), 500


@app.route('/api/tracks/<uas_id>')
def get_track(uas_id):
    """Get track for specific drone"""
    try:
        start, end = _parse_time_range(request.args)
        track = database.get_track(uas_id, start, end)
        return jsonify({
            'uas_id': uas_id,
            'track': track
        })
    except Exception as e:
        logger.error("Error getting track: %s", e)
        return jsonify({'error': str(e)}), 500


@app.route('/api/operators')
def get_operators():
    """Get operator positions"""
    try:
        start, end = _parse_time_range(request.args)
        operators = database.get_operators(start, end)
        return jsonify({'operators': operators})
    except Exception as e:
        logger.error("Error getting operators: %s", e)
        return jsonify({'error': str(e)}), 500


@app.route('/api/bounds')
def get_bounds():
    """Get bounding box of all positions in time window"""
    try:
        start, end = _parse_time_range(request.args)
        bounds = database.get_bounds(start, end)

        if bounds:
            return jsonify({
                'bounds': {
                    'min_lat': bounds[0],
                    'max_lat': bounds[1],
                    'min_lon': bounds[2],
                    'max_lon': bounds[3]
                }
            })
        else:
            return jsonify({'bounds': None})
    except Exception as e:
        logger.error("Error getting bounds: %s", e)
        return jsonify({'error': str(e)}), 500


@app.route('/api/sync', methods=['POST'])
def trigger_sync():
    """Manually trigger sync from collectors"""
    if sync_manager:
        sync_manager.force_sync()
        return jsonify({'status': 'sync triggered'})
    else:
        return jsonify({'status': 'sync disabled - no collectors configured'}), 400


def _parse_time_range(args):
    """Parse start/end time from request args"""
    end_time = datetime.now()

    # Parse end time
    end_str = args.get('end')
    if end_str:
        end_time = datetime.fromisoformat(end_str.replace('Z', '+00:00').replace('+00:00', ''))

    # Parse start time
    start_str = args.get('start')
    if start_str:
        start_time = datetime.fromisoformat(start_str.replace('Z', '+00:00').replace('+00:00', ''))
    else:
        # Default to default_hours before end
        start_time = end_time - timedelta(hours=config.default_hours)

    return start_time, end_time


def main():
    """Main entry point"""
    global config, database, sync_manager

    parser = argparse.ArgumentParser(description='Remote ID Web Interface')
    parser.add_argument('--config', required=True, help='Path to configuration YAML file')
    args = parser.parse_args()

    # Load configuration
    logger.info("Loading configuration from %s", args.config)
    config = WebConfig(args.config)

    # Initialize database
    logger.info("Initializing database at %s", config.database_path)
    database = WebDatabase(config.database_path)

    # Initialize sync manager
    sync_manager = create_sync_manager(
        database,
        config.collectors,
        config.sync_interval
    )

    if sync_manager:
        sync_manager.start()

    try:
        logger.info("Starting web server on %s:%d", config.host, config.port)
        app.run(host=config.host, port=config.port, debug=False, threaded=True)
    finally:
        if sync_manager:
            sync_manager.stop()


if __name__ == '__main__':
    main()
