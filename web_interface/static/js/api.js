/**
 * API client for Remote ID Web Interface
 */

const API = {
    baseUrl: '',

    /**
     * Initialize API client
     */
    init() {
        this.baseUrl = '';
    },

    /**
     * Get configuration
     */
    async getConfig() {
        return this._get('/api/config');
    },

    /**
     * Get list of drones in time window
     */
    async getDrones(start, end) {
        const params = new URLSearchParams();
        if (start) params.append('start', start.toISOString());
        if (end) params.append('end', end.toISOString());
        return this._get(`/api/drones?${params}`);
    },

    /**
     * Get positions in time window
     */
    async getPositions(start, end, uasId = null) {
        const params = new URLSearchParams();
        if (start) params.append('start', start.toISOString());
        if (end) params.append('end', end.toISOString());
        if (uasId) params.append('uas_id', uasId);
        return this._get(`/api/positions?${params}`);
    },

    /**
     * Get track for specific drone
     */
    async getTrack(uasId, start, end) {
        const params = new URLSearchParams();
        if (start) params.append('start', start.toISOString());
        if (end) params.append('end', end.toISOString());
        return this._get(`/api/tracks/${encodeURIComponent(uasId)}?${params}`);
    },

    /**
     * Get operator positions
     */
    async getOperators(start, end) {
        const params = new URLSearchParams();
        if (start) params.append('start', start.toISOString());
        if (end) params.append('end', end.toISOString());
        return this._get(`/api/operators?${params}`);
    },

    /**
     * Get bounds of all positions in time window
     */
    async getBounds(start, end) {
        const params = new URLSearchParams();
        if (start) params.append('start', start.toISOString());
        if (end) params.append('end', end.toISOString());
        return this._get(`/api/bounds?${params}`);
    },

    /**
     * Trigger manual sync
     */
    async triggerSync() {
        return this._post('/api/sync');
    },

    /**
     * Generic GET request
     */
    async _get(url) {
        const response = await fetch(url);
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }
        return response.json();
    },

    /**
     * Generic POST request
     */
    async _post(url, data = {}) {
        const response = await fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(data)
        });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }
        return response.json();
    }
};

// Initialize on load
API.init();
