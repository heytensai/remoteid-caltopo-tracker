/**
 * UI controller for Remote ID Web Interface
 * Handles sidebar, time picker, and user interactions
 */

const UIController = {
    // State
    currentStartTime: null,
    currentEndTime: null,
    selectedDrones: new Set(),
    isLoading: false,
    defaultHours: 24,
    droneMap: {},
    loadedTracks: new Set(),

    // DOM Elements
    elements: {},

    /**
     * Initialize UI
     */
    async init() {
        this._cacheElements();
        this._initEventListeners();
        await this._initTimePicker();
        await this._loadConfig();
        await this.refreshData();
    },

    /**
     * Cache DOM element references
     */
    _cacheElements() {
        this.elements = {
            sidebar: document.getElementById('sidebar'),
            openSidebarBtn: document.getElementById('openSidebar'),
            closeSidebarBtn: document.getElementById('closeSidebar'),
            droneList: document.getElementById('droneList'),
            refreshBtn: document.getElementById('refreshBtn'),
            startTimeInput: document.getElementById('startTime'),
            endTimeInput: document.getElementById('endTime'),
            lastUpdateSpan: document.getElementById('lastUpdate'),
            showOperatorsCheckbox: document.getElementById('showOperators'),
            showTracksCheckbox: document.getElementById('showTracks'),
            trackOpacitySlider: document.getElementById('trackOpacity'),
            timePresets: document.querySelectorAll('.time-presets button')
        };
    },

    /**
     * Initialize event listeners
     */
    _initEventListeners() {
        // Sidebar toggle
        this.elements.openSidebarBtn.addEventListener('click', () => {
            this.elements.sidebar.classList.add('open');
        });

        this.elements.closeSidebarBtn.addEventListener('click', () => {
            this.elements.sidebar.classList.remove('open');
        });

        // Refresh button
        this.elements.refreshBtn.addEventListener('click', () => {
            this.refreshData();
        });

        // Time preset buttons
        this.elements.timePresets.forEach(btn => {
            btn.addEventListener('click', (e) => {
                const hours = parseInt(e.target.dataset.hours);
                this._setTimeRange(hours);
                this._updateActivePreset(e.target);
                this.refreshData();
            });
        });

        // Show/hide operators
        this.elements.showOperatorsCheckbox.addEventListener('change', (e) => {
            MapController.toggleOperators(e.target.checked);
        });

        // Show/hide tracks
        this.elements.showTracksCheckbox.addEventListener('change', (e) => {
            MapController.toggleTracks(e.target.checked);
        });

        // Track opacity
        let opacityTimeout = null;
        this.elements.trackOpacitySlider.addEventListener('input', (e) => {
            if (opacityTimeout) {
                clearTimeout(opacityTimeout);
            }
            opacityTimeout = setTimeout(() => {
                MapController.setTrackOpacity(e.target.value);
            }, 50);
        });

        // Close sidebar when clicking on map (mobile)
        document.addEventListener('click', (e) => {
            if (window.innerWidth < 768 &&
                !this.elements.sidebar.contains(e.target) &&
                !this.elements.openSidebarBtn.contains(e.target)) {
                this.elements.sidebar.classList.remove('open');
            }
        });
    },

    /**
     * Initialize Flatpickr time pickers
     */
    async _initTimePicker() {
        const endTime = new Date();
        const startTime = new Date(endTime.getTime() - this.defaultHours * 60 * 60 * 1000);

        this.currentStartTime = startTime;
        this.currentEndTime = endTime;

        const config = {
            enableTime: true,
            dateFormat: 'Y-m-d H:i',
            time_24hr: true,
            onChange: (selectedDates, dateStr, instance) => {
                // Clear active preset when manual time is selected
                this._clearActivePreset();
                if (instance.element.id === 'startTime') {
                    this.currentStartTime = selectedDates[0];
                } else {
                    this.currentEndTime = selectedDates[0];
                }
            }
        };

        flatpickr(this.elements.startTimeInput, {
            ...config,
            defaultDate: startTime
        });

        flatpickr(this.elements.endTimeInput, {
            ...config,
            defaultDate: endTime
        });
    },

    /**
     * Load configuration from server
     */
    async _loadConfig() {
        try {
            const config = await API.getConfig();
            this.defaultHours = config.default_hours || 24;

            // Re-initialize time picker with correct default
            this._setTimeRange(this.defaultHours);
        } catch (e) {
            console.error('Failed to load config:', e);
        }
    },

    /**
     * Set time range based on hours back from now
     */
    _setTimeRange(hours) {
        const endTime = new Date();
        const startTime = new Date(endTime.getTime() - hours * 60 * 60 * 1000);

        this.currentStartTime = startTime;
        this.currentEndTime = endTime;

        // Update Flatpickr instances
        if (this.elements.startTimeInput._flatpickr) {
            this.elements.startTimeInput._flatpickr.setDate(startTime);
        }
        if (this.elements.endTimeInput._flatpickr) {
            this.elements.endTimeInput._flatpickr.setDate(endTime);
        }
    },

    /**
     * Update active preset button
     */
    _updateActivePreset(activeBtn) {
        this.elements.timePresets.forEach(btn => btn.classList.remove('active'));
        activeBtn.classList.add('active');
    },

    /**
     * Clear active preset
     */
    _clearActivePreset() {
        this.elements.timePresets.forEach(btn => btn.classList.remove('active'));
    },

    /**
     * Refresh all data
     */
    async refreshData() {
        if (this.isLoading) return;

        this.isLoading = true;
        this.elements.refreshBtn.classList.add('spinning');

        try {
            // Fetch drones
            const dronesResponse = await API.getDrones(this.currentStartTime, this.currentEndTime);
            const drones = dronesResponse.drones || [];

            // Update drone list
            this._updateDroneList(drones);

            // Update map markers
            MapController.updateDrones(drones);

            // Store drone data for lazy track loading
            this.droneMap = {};
            drones.forEach(d => { this.droneMap[d.uas_id] = d; });

            // Fetch operators
            const operatorsResponse = await API.getOperators(this.currentStartTime, this.currentEndTime);
            MapController.updateOperators(operatorsResponse.operators || []);

            // Try to fit bounds if no default center is set
            if (!MapController.config.center_lat) {
                const boundsResponse = await API.getBounds(this.currentStartTime, this.currentEndTime);
                if (boundsResponse.bounds) {
                    MapController.fitBounds(boundsResponse.bounds);
                }
            }

            // Update last update time
            this._updateLastUpdateTime();

        } catch (e) {
            console.error('Failed to refresh data:', e);
            this._showError('Failed to load data. Please try again.');
        } finally {
            this.isLoading = false;
            this.elements.refreshBtn.classList.remove('spinning');
        }
    },

    /**
     * Update the drone list in sidebar
     */
    _updateDroneList(drones) {
        const list = this.elements.droneList;

        if (drones.length === 0) {
            list.innerHTML = `
                <div class="empty-state">
                    <i class="fas fa-satellite-dish"></i>
                    <p>No drones detected in time window</p>
                </div>
            `;
            return;
        }

        // Group drones by date
        const groups = {};
        drones.forEach(drone => {
            const time = new Date(drone.timestamp);
            const dateKey = `${time.getFullYear()}-${String(time.getMonth() + 1).padStart(2, '0')}-${String(time.getDate()).padStart(2, '0')}`;
            if (!groups[dateKey]) {
                groups[dateKey] = [];
            }
            groups[dateKey].push(drone);
        });

        const sortedDates = Object.keys(groups).sort().reverse();

        let html = '';
        sortedDates.forEach(date => {
            const droneCount = groups[date].length;
            html += `
                <div class="date-group">
                    <div class="date-header">
                        <span class="date-label">${date}</span>
                        <span class="date-count">${droneCount} drone${droneCount !== 1 ? 's' : ''}</span>
                        <i class="fas fa-chevron-right date-chevron"></i>
                    </div>
                    <div class="date-items collapsed">
                        ${groups[date].map(drone => {
                            const color = MapController.getDroneColor(drone.uas_id);
                            const altitude = drone.altitude !== null && drone.altitude !== undefined
                                ? `${drone.altitude.toFixed(0)}m`
                                : 'N/A';
                            const time = new Date(drone.timestamp);
                            const timeStr = time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });

                            const isSelected = this.selectedDrones.has(drone.uas_id);

                            return `
                                <div class="drone-item ${isSelected ? 'active' : ''}" data-uas-id="${drone.uas_id}">
                                    <div class="drone-color" style="background-color: ${color};"></div>
                                    <div class="drone-info">
                                        <div class="drone-id">${drone.uas_id}</div>
                                        <div class="drone-meta">Alt: ${altitude} | ${timeStr}</div>
                                    </div>
                                    <div class="drone-actions">
                                        <button class="focus-btn" title="Focus on map">
                                            <i class="fas fa-crosshairs"></i>
                                        </button>
                                    </div>
                                </div>
                            `;
                        }).join('')}
                    </div>
                </div>
            `;
        });

        list.innerHTML = html;

        // Add date header click handlers (toggle expand/collapse)
        list.querySelectorAll('.date-header').forEach(header => {
            header.addEventListener('click', async () => {
                const group = header.closest('.date-group');
                const items = group.querySelector('.date-items');
                const chevron = header.querySelector('.date-chevron');

                if (items.classList.contains('collapsed')) {
                    items.classList.remove('collapsed');
                    chevron.classList.remove('fa-chevron-right');
                    chevron.classList.add('fa-chevron-down');

                    // Load tracks for drones in this group
                    const droneItems = items.querySelectorAll('.drone-item');
                    const uasIds = Array.from(droneItems).map(di => di.dataset.uasId);
                    for (const uasId of uasIds) {
                        if (!this.loadedTracks.has(uasId)) {
                            await MapController.loadTrack(uasId, this.currentStartTime, this.currentEndTime);
                            this.loadedTracks.add(uasId);
                        }
                    }
                } else {
                    items.classList.add('collapsed');
                    chevron.classList.remove('fa-chevron-down');
                    chevron.classList.add('fa-chevron-right');

                    // Remove tracks for drones in this group
                    const droneItems = items.querySelectorAll('.drone-item');
                    const uasIds = Array.from(droneItems).map(di => di.dataset.uasId);
                    for (const uasId of uasIds) {
                        MapController.removeTrack(uasId);
                        this.loadedTracks.delete(uasId);
                    }
                }
            });
        });

        // Add click handlers for drone items
        list.querySelectorAll('.drone-item').forEach(item => {
            item.addEventListener('click', (e) => {
                const uasId = item.dataset.uasId;

                // Toggle selection
                if (this.selectedDrones.has(uasId)) {
                    this.selectedDrones.delete(uasId);
                    item.classList.remove('active');
                } else {
                    this.selectedDrones.clear();
                    this.selectedDrones.add(uasId);
                    list.querySelectorAll('.drone-item').forEach(i => i.classList.remove('active'));
                    item.classList.add('active');
                }

                // Focus on map
                MapController.highlightDrone(uasId);

                // Load track if not already loaded
                if (!this.loadedTracks.has(uasId)) {
                    MapController.loadTrack(uasId, this.currentStartTime, this.currentEndTime);
                    this.loadedTracks.add(uasId);
                }

                // Close sidebar on mobile
                if (window.innerWidth < 768) {
                    this.elements.sidebar.classList.remove('open');
                }
            });
        });

        // Add focus button handlers
        list.querySelectorAll('.focus-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const uasId = btn.closest('.drone-item').dataset.uasId;
                MapController.panToDrone(uasId);

                // Close sidebar on mobile
                if (window.innerWidth < 768) {
                    this.elements.sidebar.classList.remove('open');
                }
            });
        });
    },

    /**
     * Update last update time display
     */
    _updateLastUpdateTime() {
        const now = new Date();
        this.elements.lastUpdateSpan.textContent = `Last updated: ${now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })}`;
    },

    /**
     * Show error message
     */
    _showError(message) {
        // Simple alert for now - could be replaced with a toast
        console.error(message);
        // Could implement a toast notification here
    }
};

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    UIController.init();
});
