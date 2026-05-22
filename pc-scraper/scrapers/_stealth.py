"""
Stealth init script untuk Playwright headless context.
========================================================

Patch JavaScript runtime supaya context Playwright tidak ke-detect
sebagai automation oleh anti-bot fingerprinting (FingerprintJS, Imperva,
Cloudflare bot management, dll).

Signal yang di-patch:
  1. navigator.webdriver               → false (default true di Playwright)
  2. navigator.plugins                  → fake [PDF Viewer, Chrome PDF, Chromium PDF]
  3. navigator.languages                → ['id-ID', 'id', 'en-US', 'en']
  4. WebGL UNMASKED_VENDOR/RENDERER     → spoof Intel ANGLE (default: SwiftShader)
  5. window.chrome.{runtime, loadTimes, csi}  → minimal stub
  6. Permissions API notifications quirk → patch denied/default mismatch
  7. Hide __playwright__ globals

Cara pakai:
    from .stealth import STEALTH_INIT_JS, STEALTH_LAUNCH_ARGS
    
    browser = pw.chromium.launch(
        headless=True,
        args=STEALTH_LAUNCH_ARGS,  # selain default args yang lain
        ignore_default_args=['--enable-automation'],
    )
    context = browser.new_context(...)
    context.add_init_script(STEALTH_INIT_JS)

Note: Stealth ini cukup untuk login flow & low-volume scraping. Untuk
volume tinggi (scraping ratusan akun parallel), pertimbangkan playwright-
stealth library + residential proxy + fingerprint randomization yang lebih
aggressive (canvas noise, audio context, hardware concurrency, dll).
"""

# Args yang nutup signal otomasi paling jelas. Kombinasi dengan STEALTH_INIT_JS
# di context.add_init_script().
STEALTH_LAUNCH_ARGS = [
    '--disable-blink-features=AutomationControlled',
    '--disable-dev-shm-usage',
    '--no-sandbox',
    '--no-default-browser-check',
    '--no-first-run',
    '--no-service-autorun',
    '--password-store=basic',
    '--use-mock-keychain',
    '--disable-features=IsolateOrigins,site-per-process,AutomationControlled',
    '--enable-webgl',
    '--use-gl=angle',
    '--disable-background-timer-throttling',
    '--disable-backgrounding-occluded-windows',
    '--disable-renderer-backgrounding',
    '--disable-ipc-flooding-protection',
    '--disable-gpu',
]

# Init script yang di-inject SEBELUM page navigation. Patch semua property
# JavaScript yang kebanyakan anti-bot tools pakai untuk fingerprint.
#
# Sumber referensi: puppeteer-extra-plugin-stealth + playwright-stealth.
# Kita inline manual karena (a) gak mau add dependency tambahan, (b) cukup
# untuk login flow + low-volume scraping yang kita target.
STEALTH_INIT_JS = r"""
(() => {
  // ==== 1. navigator.webdriver — anti-bot signal #1 ====
  try {
    Object.defineProperty(Navigator.prototype, 'webdriver', {
      get: () => false,
      configurable: true,
    });
  } catch (e) {}

  // ==== 2. navigator.plugins — Chrome real punya minimal PDF Viewer ====
  try {
    const makeMime = (type, suffixes, description) => ({
      type, suffixes, description, enabledPlugin: null,
    });
    const makePlugin = (name, filename, description, mimeTypes) => {
      const plugin = {
        name, filename, description, length: mimeTypes.length,
      };
      mimeTypes.forEach((mt, i) => {
        plugin[i] = mt;
        plugin[mt.type] = mt;
        mt.enabledPlugin = plugin;
      });
      return plugin;
    };
    const pdf = makeMime('application/pdf', 'pdf', 'Portable Document Format');
    const pdfViewer = makePlugin(
      'PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format', [pdf]
    );
    const chromePdf = makePlugin(
      'Chrome PDF Viewer', 'internal-pdf-viewer',
      'Portable Document Format', [pdf]
    );
    const chromiumPdf = makePlugin(
      'Chromium PDF Viewer', 'internal-pdf-viewer',
      'Portable Document Format', [pdf]
    );
    const arr = [pdfViewer, chromePdf, chromiumPdf];
    arr.item = (i) => arr[i] || null;
    arr.namedItem = (n) => arr.find(p => p.name === n) || null;
    arr.refresh = () => {};
    Object.setPrototypeOf(arr, PluginArray.prototype);
    Object.defineProperty(Navigator.prototype, 'plugins', {
      get: () => arr,
      configurable: true,
    });
  } catch (e) {}

  // ==== 3. navigator.languages — sesuaikan dengan locale context ====
  try {
    Object.defineProperty(Navigator.prototype, 'languages', {
      get: () => ['id-ID', 'id', 'en-US', 'en'],
      configurable: true,
    });
  } catch (e) {}

  // ==== 4. WebGL vendor/renderer spoof ====
  // Headless / Docker default ke SwiftShader. Real desktop punya ANGLE+Intel.
  try {
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(parameter) {
      if (parameter === 37445) return 'Google Inc. (Intel)';
      if (parameter === 37446) {
        return 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)';
      }
      return getParameter.apply(this, arguments);
    };
    if (typeof WebGL2RenderingContext !== 'undefined') {
      const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
      WebGL2RenderingContext.prototype.getParameter = function(parameter) {
        if (parameter === 37445) return 'Google Inc. (Intel)';
        if (parameter === 37446) {
          return 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)';
        }
        return getParameter2.apply(this, arguments);
      };
    }
  } catch (e) {}

  // ==== 5. window.chrome stub ====
  try {
    if (!window.chrome) {
      window.chrome = {};
    }
    if (!window.chrome.runtime) {
      window.chrome.runtime = {
        OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', UPDATE: 'update' },
        PlatformOs: { WIN: 'win', MAC: 'mac', LINUX: 'linux' },
        connect: () => ({ onMessage: { addListener: () => {} }, postMessage: () => {} }),
        sendMessage: () => {},
      };
    }
    if (!window.chrome.loadTimes) {
      window.chrome.loadTimes = () => ({
        commitLoadTime: Date.now() / 1000,
        connectionInfo: 'h2',
        finishDocumentLoadTime: Date.now() / 1000,
        finishLoadTime: Date.now() / 1000,
        firstPaintAfterLoadTime: 0,
        firstPaintTime: Date.now() / 1000,
        navigationType: 'Other',
        npnNegotiatedProtocol: 'h2',
        requestTime: Date.now() / 1000 - 1,
        startLoadTime: Date.now() / 1000 - 1,
        wasAlternateProtocolAvailable: false,
        wasFetchedViaSpdy: true,
        wasNpnNegotiated: true,
      });
    }
    if (!window.chrome.csi) {
      window.chrome.csi = () => ({
        onloadT: Date.now(), pageT: Date.now() - 1, startE: Date.now() - 2, tran: 15,
      });
    }
  } catch (e) {}

  // ==== 6. Permissions API quirk ====
  try {
    const origQuery = window.navigator.permissions && window.navigator.permissions.query;
    if (origQuery) {
      window.navigator.permissions.query = (parameters) => (
        parameters && parameters.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission, onchange: null })
          : origQuery(parameters)
      );
    }
  } catch (e) {}

  // ==== 7. Hide playwright-specific globals ====
  try {
    delete window.__playwright__;
    delete window.__pwInitScripts__;
    delete window.__PLAYWRIGHT_GUID__;
  } catch (e) {}
})();
"""
