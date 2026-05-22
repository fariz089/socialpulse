// frontend/command-center/cc-common.js
// Shared library untuk semua monitor view di /command-center/*
// Diload via <script src="cc-common.js"></script> di setiap HTML monitor.

(function (window) {
  'use strict';

  // -----------------------------
  // Config
  // -----------------------------
  const PARAMS = new URLSearchParams(window.location.search);
  const MONITOR_ID = parseInt(PARAMS.get('monitor_id') || '0', 10);
  const KEYWORD = PARAMS.get('keyword') || '';
  const PROJECT_ID = PARAMS.get('project_id') || '';
  const TOKEN = localStorage.getItem('jwt') || PARAMS.get('token') || '';

  window.CC = {
    monitorId: MONITOR_ID,
    keyword: KEYWORD,
    projectId: PROJECT_ID,
    token: TOKEN,
  };

  // -----------------------------
  // Toast
  // -----------------------------
  function ensureToastContainer() {
    let c = document.getElementById('cc-toasts');
    if (!c) {
      c = document.createElement('div');
      c.id = 'cc-toasts';
      c.style.cssText = 'position:fixed;top:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:8px;';
      document.body.appendChild(c);
    }
    return c;
  }
  window.CC.toast = function (level, msg) {
    const c = ensureToastContainer();
    const e = document.createElement('div');
    const colors = {
      success: '#10b981', error: '#ef4444', warn: '#f59e0b', info: '#3b82f6',
    };
    e.style.cssText = `background:${colors[level] || colors.info};color:#fff;padding:10px 14px;border-radius:8px;font-size:13px;box-shadow:0 4px 12px rgba(0,0,0,.3);max-width:340px;`;
    e.textContent = msg;
    c.appendChild(e);
    setTimeout(() => e.remove(), 4000);
  };

  // -----------------------------
  // WebSocket Client (Fase 4)
  // -----------------------------
  class SlayticsWS {
    constructor(monitorId) {
      this.monitorId = monitorId;
      this.handlers = new Map();
      this.ws = null;
      this.reconnectDelay = 1000;
      this.maxReconnectDelay = 30000;
      this._closed = false;
      this._heartbeat = null;
    }

    connect() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const params = new URLSearchParams({
        monitor_id: String(this.monitorId),
      });
      if (TOKEN) params.set('token', TOKEN);
      const url = `${proto}://${location.host}/ws?${params}`;

      try {
        this.ws = new WebSocket(url);
      } catch (e) {
        console.error('[WS] connect error:', e);
        this._scheduleReconnect();
        return;
      }

      this.ws.addEventListener('open', () => {
        console.log(`[WS] Monitor ${this.monitorId} connected`);
        this.reconnectDelay = 1000;
        this._heartbeat = setInterval(() => {
          if (this.ws?.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ type: 'ping' }));
          }
        }, 30000);
        this._fire('open', {});
      });

      this.ws.addEventListener('message', (e) => {
        try {
          const msg = JSON.parse(e.data);
          if (msg.type) this._fire(msg.type, msg);
          this._fire('*', msg);
        } catch (err) {
          console.warn('[WS] invalid message:', err);
        }
      });

      this.ws.addEventListener('close', (ev) => {
        clearInterval(this._heartbeat);
        this._fire('close', { code: ev.code, reason: ev.reason });
        if (this._closed) return;
        this._scheduleReconnect();
      });

      this.ws.addEventListener('error', (err) => {
        console.warn('[WS] error:', err);
      });
    }

    _scheduleReconnect() {
      console.log(`[WS] reconnect in ${this.reconnectDelay}ms`);
      setTimeout(() => this.connect(), this.reconnectDelay);
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, this.maxReconnectDelay);
    }

    on(type, fn) {
      this.handlers.set(type, fn);
      return this;
    }

    _fire(type, msg) {
      const h = this.handlers.get(type);
      if (h) {
        try { h(msg); } catch (e) { console.error(`[WS] handler ${type} error:`, e); }
      }
    }

    close() {
      this._closed = true;
      clearInterval(this._heartbeat);
      if (this.ws) this.ws.close();
    }
  }
  window.CC.SlayticsWS = SlayticsWS;

  // -----------------------------
  // Connection status pill
  // -----------------------------
  window.CC.injectStatusPill = function (ws) {
    let pill = document.getElementById('cc-status-pill');
    if (!pill) {
      pill = document.createElement('div');
      pill.id = 'cc-status-pill';
      pill.style.cssText = 'position:fixed;top:8px;right:12px;padding:3px 9px;border-radius:999px;font-size:10px;font-family:ui-monospace,monospace;letter-spacing:.5px;background:rgba(0,0,0,.6);color:#fff;z-index:9000;display:flex;align-items:center;gap:6px;border:1px solid rgba(255,255,255,.08);';
      document.body.appendChild(pill);
    }
    function setStatus(state, text) {
      const dot = state === 'ok' ? '🟢' : state === 'warn' ? '🟡' : '🔴';
      pill.textContent = `${dot} M${MONITOR_ID} · ${text}`;
    }
    ws.on('open', () => setStatus('ok', 'connected'));
    ws.on('close', () => setStatus('error', 'disconnected'));
    ws.on('connected', () => setStatus('ok', 'live'));
    setStatus('warn', 'connecting...');
  };

  // -----------------------------
  // Header (judul keyword + clock)
  // -----------------------------
  window.CC.injectHeader = function (title, subtitle) {
    let h = document.getElementById('cc-header');
    if (!h) {
      h = document.createElement('header');
      h.id = 'cc-header';
      h.style.cssText = 'padding:14px 18px 14px 18px;padding-right:140px;border-bottom:1px solid rgba(255,255,255,.08);display:flex;justify-content:space-between;align-items:center;position:relative;';
      document.body.insertBefore(h, document.body.firstChild);
    }
    h.innerHTML = `
      <div>
        <div style="font-size:12px;letter-spacing:1px;text-transform:uppercase;opacity:.5;">${title}</div>
        <div style="font-size:18px;font-weight:600;">${subtitle || (KEYWORD ? '#' + KEYWORD : '— belum ada keyword —')}</div>
      </div>
      <div id="cc-clock" style="font-family:ui-monospace,monospace;opacity:.7;font-size:13px;"></div>
    `;
    setInterval(() => {
      const c = document.getElementById('cc-clock');
      if (c) c.textContent = new Date().toLocaleTimeString();
    }, 1000);
  };

  // -----------------------------
  // Empty state
  // -----------------------------
  window.CC.emptyState = function (msg) {
    return `<div style="display:flex;align-items:center;justify-content:center;height:100%;opacity:.4;font-size:14px;text-align:center;padding:20px;">${msg}</div>`;
  };

  // -----------------------------
  // Number formatter
  // -----------------------------
  window.CC.fmt = function (n) {
    if (n == null) return '—';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return String(n);
  };

  // -----------------------------
  // API helper
  // -----------------------------
  window.CC.api = function (path, opts = {}) {
    const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
    if (TOKEN) headers['Authorization'] = `Bearer ${TOKEN}`;
    return fetch(path, { ...opts, headers }).then(r => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    });
  };

  // -----------------------------
  // Auto-load data dari /api/monitor/data
  // Dipakai oleh setiap monitor saat boot — fetch sekali, render, lalu listen WS.
  // Tanpa ini monitor cuma blank menunggu push dari JARVIS.
  // -----------------------------
  window.CC.fetchMonitorData = async function () {
    if (!KEYWORD && !PROJECT_ID) return null;
    const params = new URLSearchParams();
    if (PROJECT_ID) params.set('project_id', PROJECT_ID);
    if (KEYWORD) params.set('keyword', KEYWORD);
    try {
      return await window.CC.api(`/api/monitor/data?${params}`);
    } catch (e) {
      console.warn('[CC.fetchMonitorData] failed:', e.message);
      return null;
    }
  };

  // -----------------------------
  // Auto-refresh setiap kali scheduler selesai cycle
  // Pasang sekali per monitor, callback yang dikasih akan dipanggil dengan data fresh.
  // -----------------------------
  window.CC.autoRefreshOnSchedulerComplete = function (ws, onData) {
    ws.on('scheduler_complete', async () => {
      const d = await window.CC.fetchMonitorData();
      if (d && typeof onData === 'function') onData(d);
    });
  };

  // -----------------------------
  // Boot guard — alert kalau monitor_id invalid
  // -----------------------------
  window.CC.requireMonitorId = function () {
    if (!MONITOR_ID || MONITOR_ID < 1 || MONITOR_ID > 6) {
      document.body.innerHTML = `<div style="padding:40px;font-family:system-ui;color:#ef4444;">
        <h1>⚠ Invalid monitor_id</h1>
        <p>Buka URL ini dengan query <code>?monitor_id=1</code> sampai <code>?monitor_id=6</code>.</p>
        <p>Contoh: <code>${location.pathname}?monitor_id=1&keyword=msglow</code></p>
      </div>`;
      return false;
    }
    return true;
  };

})(window);
