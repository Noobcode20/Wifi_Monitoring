// Dashboard Tab Component

import { healthService } from '../services/health.service.js';
import { sensingService } from '../services/sensing.service.js';

export class DashboardTab {
  static STREAM_HEALTH_REFRESH_DELAY_MS = 500;

  constructor(containerElement) {
    this.container = containerElement;
    this.healthSubscription = null;
  }

  // Initialize component
  async init() {
    this.cacheElements();
    await this.loadInitialData();
    this.startMonitoring();
  }

  // Cache DOM elements
  cacheElements() {
    // Status indicators
    this.statusElements = {
      apiStatus: this.container.querySelector('.api-status'),
      streamStatus: this.container.querySelector('.stream-status'),
      hardwareStatus: this.container.querySelector('.hardware-status')
    };
  }

  // Load initial data
  async loadInitialData() {
    try {
      // Get API info
      const info = await healthService.getApiInfo();
      this.updateApiInfo(info);

    } catch (error) {
      // A status endpoint may be unavailable while the packet monitor runs.
      console.log('Dashboard: health API unavailable');
    }
  }

  // Start monitoring
  startMonitoring() {
    // Subscribe to health updates
    this.healthSubscription = healthService.subscribeToHealth(health => {
      this.updateHealthStatus(health);
    });

    // Subscribe to sensing service state changes for data source indicator
    this._sensingUnsub = sensingService.onStateChange((state) => {
      this.updateDataSourceIndicator();
      this._refreshStreamHealthOn(state);
    });
    // Also update on data — catches source changes mid-stream
    this._sensingDataUnsub = sensingService.onData(() => {
      this.updateDataSourceIndicator();
    });
    // Initial update
    this.updateDataSourceIndicator();

    // Start health monitoring
    healthService.startHealthMonitoring(30000);
  }

  // #2087: the Streaming card comes from /health, polled every 30 s. The first
  // poll runs before this page's own socket has connected (with auth on, the
  // ticket round-trip makes that gap wider), so the card read "IDLE, 0
  // client(s)" for up to 30 s while data was streaming. Re-read health when
  // the socket opens or drops. The short delay lets the server register the
  // socket before it is counted.
  _refreshStreamHealthOn(state) {
    if (state === this._lastSensingState) return;
    const wasConnected = this._lastSensingState === 'connected';
    this._lastSensingState = state;
    if (state !== 'connected' && !wasConnected) return;
    clearTimeout(this._streamHealthTimer);
    this._streamHealthTimer = setTimeout(() => {
      healthService.getSystemHealth().catch(() => { /* next poll retries */ });
    }, DashboardTab.STREAM_HEALTH_REFRESH_DELAY_MS);
  }

  // Update the data source indicator on the dashboard
  updateDataSourceIndicator() {
    const el = this.container.querySelector('#dashboard-datasource');
    if (!el) return;
    const ds = sensingService.dataSource;
    const statusText = el.querySelector('.status-text');
    const statusMsg  = el.querySelector('.status-message');
    const config = {
      'live':              { text: 'CSI LIVE',  status: 'healthy', msg: 'ESP32 signal stream is live; presence is an unvalidated heuristic' },
      'server-simulated':  { text: 'SIMULATED', status: 'warning', msg: 'Server running without hardware' },
      'reconnecting':      { text: 'RECONNECTING', status: 'degraded', msg: 'Attempting to connect...' },
      'unreachable':       { text: 'NO DATA',   status: 'unhealthy', msg: 'Server unreachable — readings below are stale' },
      'simulated':         { text: 'INVENTED',  status: 'unhealthy', msg: 'Browser-generated data, not measured' },
      'auth-required':     { text: 'TOKEN REQUIRED', status: 'unhealthy', msg: 'Paste the server API token in Settings \u2192 API Access' },
    };
    const cfg = config[ds] || config['reconnecting'];
    el.className = `component-status status-${cfg.status}`;
    if (statusText) statusText.textContent = cfg.text;
    if (statusMsg)  statusMsg.textContent = cfg.msg;
  }

  // Update API info display
  updateApiInfo(info) {
    // Update version
    const versionElement = this.container.querySelector('.api-version');
    if (versionElement && info.version) {
      versionElement.textContent = `v${info.version}`;
    }

    // Update environment
    const envElement = this.container.querySelector('.api-environment');
    if (envElement && info.environment) {
      envElement.textContent = info.environment;
      envElement.className = `api-environment env-${info.environment}`;
    }

  }

  // Update health status
  updateHealthStatus(health) {
    if (!health) return;

    // Update overall status
    const overallStatus = this.container.querySelector('.overall-health');
    if (overallStatus) {
      overallStatus.className = `overall-health status-${health.status}`;
      overallStatus.textContent = health.status.toUpperCase();
    }

    // Update component statuses
    if (health.components) {
      Object.entries(health.components).forEach(([component, status]) => {
        this.updateComponentStatus(component, status);
      });
    }

  }

  // Update component status
  updateComponentStatus(component, status) {
    // Map backend component names to UI component names
    const componentMap = {
      'stream': 'streaming',
      'hardware': 'hardware'
    };
    
    const uiComponent = componentMap[component] || component;
    const element = this.container.querySelector(`[data-component="${uiComponent}"]`);
    
    if (element) {
      element.className = `component-status status-${status.status}`;
      const statusText = element.querySelector('.status-text');
      const statusMessage = element.querySelector('.status-message');
      
      if (statusText) {
        statusText.textContent = status.status.toUpperCase();
      }
      
      if (statusMessage && status.message) {
        statusMessage.textContent = status.message;
      }
    }
    
    // Also update API status based on overall health
    if (component === 'hardware') {
      const apiElement = this.container.querySelector(`[data-component="api"]`);
      if (apiElement) {
        apiElement.className = `component-status status-healthy`;
        const apiStatusText = apiElement.querySelector('.status-text');
        const apiStatusMessage = apiElement.querySelector('.status-message');
        
        if (apiStatusText) {
          apiStatusText.textContent = 'HEALTHY';
        }
        
        if (apiStatusMessage) {
          apiStatusMessage.textContent = 'API server is running normally';
        }
      }
    }
  }

  // Show error message
  showError(message) {
    const errorContainer = this.container.querySelector('.error-container');
    if (errorContainer) {
      errorContainer.textContent = message;
      errorContainer.style.display = 'block';
      
      setTimeout(() => {
        errorContainer.style.display = 'none';
      }, 5000);
    }
  }

  // Clean up
  dispose() {
    if (this.healthSubscription) {
      this.healthSubscription();
    }
    if (this._sensingUnsub) this._sensingUnsub();
    clearTimeout(this._streamHealthTimer);
    if (this._sensingDataUnsub) this._sensingDataUnsub();


    healthService.stopHealthMonitoring();
  }
}
