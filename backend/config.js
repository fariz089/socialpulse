// backend/config.js
// Unified config reader: pertama coba MongoDB collection 'app_settings',
// kalau kosong fallback ke process.env. Caching 30 detik supaya gak hammer DB.
//
// Hot-reload: panggil invalidate() setelah PUT /api/settings supaya next read
// langsung ambil yang baru, bukan dari cache.
//
// Skema doc di MongoDB:
//   { _id: 'app_settings', settings: { key: value, ... }, updated_at: Date }

const CACHE_TTL_MS = 30 * 1000;

let _db = null;
let _cache = null;
let _cachedAt = 0;

// Default values — dipakai kalau env DAN db dua-duanya kosong.
const DEFAULTS = {
  // AI Agent
  openrouter_api_key: '',
  openrouter_model: 'google/gemini-2.5-flash',
  openrouter_referer: 'http://localhost',
  agent_max_iterations: 12,
  agent_rate_limit: 10,

  // Scheduler
  scheduler_enabled: true,
  scheduler_interval_minutes: 60,
  scheduler_max_results: 30,

  // Alert thresholds
  alert_negative_threshold: 30,
  alert_volume_multiplier: 3,
  alert_cooldown_minutes: 120,

  // Telegram
  telegram_bot_token: '',
  telegram_chat_id: '',

  // Discord
  discord_webhook_url: '',

  // Scraper override
  pc_scraper_url: '',

  // WebSocket
  ws_require_auth: false,
};

// Mapping nama UI/DB ke nama env (untuk migrasi & fallback)
const ENV_MAPPING = {
  openrouter_api_key: 'OPENROUTER_API_KEY',
  openrouter_model: 'OPENROUTER_MODEL',
  openrouter_referer: 'OPENROUTER_REFERER',
  agent_max_iterations: 'AGENT_MAX_ITERATIONS',
  agent_rate_limit: 'AGENT_RATE_LIMIT',
  scheduler_enabled: 'SCHEDULER_ENABLED',
  scheduler_interval_minutes: 'SCHEDULER_INTERVAL_MINUTES',
  scheduler_max_results: 'SCHEDULER_MAX_RESULTS',
  alert_negative_threshold: 'ALERT_NEGATIVE_THRESHOLD',
  alert_volume_multiplier: 'ALERT_VOLUME_MULTIPLIER',
  alert_cooldown_minutes: 'ALERT_COOLDOWN_MINUTES',
  telegram_bot_token: 'TELEGRAM_BOT_TOKEN',
  telegram_chat_id: 'TELEGRAM_CHAT_ID',
  discord_webhook_url: 'DISCORD_WEBHOOK_URL',
  pc_scraper_url: 'PC_SCRAPER_URL',
  ws_require_auth: 'WS_REQUIRE_AUTH',
};

// Type coercion — DB & env sama-sama string by default, kita normalize.
function coerce(key, value) {
  if (value === null || value === undefined || value === '') {
    return DEFAULTS[key];
  }

  const isInt = ['agent_max_iterations', 'agent_rate_limit', 'scheduler_interval_minutes',
    'scheduler_max_results', 'alert_cooldown_minutes'].includes(key);
  const isFloat = ['alert_negative_threshold', 'alert_volume_multiplier'].includes(key);
  const isBool = ['scheduler_enabled', 'ws_require_auth'].includes(key);

  if (isInt) {
    const n = parseInt(value, 10);
    return Number.isFinite(n) ? n : DEFAULTS[key];
  }
  if (isFloat) {
    const n = parseFloat(value);
    return Number.isFinite(n) ? n : DEFAULTS[key];
  }
  if (isBool) {
    if (typeof value === 'boolean') return value;
    const s = String(value).toLowerCase().trim();
    return !(s === '0' || s === 'false' || s === 'no' || s === '');
  }
  return String(value);
}

// Initialize — call this once after db is connected.
function init(db) {
  _db = db;
}

// Force re-read on next access.
function invalidate() {
  _cache = null;
  _cachedAt = 0;
}

// Load all settings from DB doc + env fallback. Cached.
async function loadAll() {
  const now = Date.now();
  if (_cache && (now - _cachedAt) < CACHE_TTL_MS) return _cache;

  const merged = {};
  let dbSettings = {};
  if (_db) {
    try {
      const doc = await _db.collection('app_settings').findOne({ _id: 'app_settings' });
      dbSettings = doc?.settings || {};
    } catch (e) {
      console.warn('[config] DB read failed, fallback to env only:', e.message);
    }
  }

  for (const key of Object.keys(DEFAULTS)) {
    // Priority: DB → env → default
    if (dbSettings.hasOwnProperty(key) && dbSettings[key] !== '' && dbSettings[key] !== null) {
      merged[key] = coerce(key, dbSettings[key]);
    } else {
      const envName = ENV_MAPPING[key];
      const envValue = envName ? process.env[envName] : undefined;
      merged[key] = coerce(key, envValue);
    }
  }

  _cache = merged;
  _cachedAt = now;
  return merged;
}

// Convenient sync getter — relies on cache being warm.
// Call await loadAll() once at boot to warm cache.
function get(key) {
  if (!_cache) return DEFAULTS[key];  // very early in boot
  return _cache[key] !== undefined ? _cache[key] : DEFAULTS[key];
}

// Save partial settings to DB. Pass object { key1: val1, key2: val2 }.
// Validate types before save. Return updated full settings.
async function save(partial) {
  if (!_db) throw new Error('config not initialized');

  // Validate keys
  for (const key of Object.keys(partial)) {
    if (!DEFAULTS.hasOwnProperty(key)) {
      throw new Error(`unknown setting key: ${key}`);
    }
  }

  // Validate scheduler interval — minimum 5 to prevent rate-limit & overlap
  // Frontend WAJIB tampilkan warning untuk value < 5; backend tetap terima sampai 1
  // (user yang ngotot bisa override, tapi resiko di mereka).
  if (partial.scheduler_interval_minutes !== undefined) {
    const v = parseInt(partial.scheduler_interval_minutes, 10);
    if (!Number.isFinite(v) || v < 1) {
      throw new Error('scheduler_interval_minutes must be >= 1');
    }
  }

  // Build update doc
  const setOps = {};
  for (const key of Object.keys(partial)) {
    setOps[`settings.${key}`] = partial[key];
  }
  setOps['updated_at'] = new Date();

  await _db.collection('app_settings').updateOne(
    { _id: 'app_settings' },
    { $set: setOps },
    { upsert: true }
  );

  invalidate();
  return await loadAll();
}

// Migrasi: kalau collection app_settings kosong, populate dari env saat boot.
// Dipanggil sekali setelah init(db).
async function migrateFromEnv() {
  if (!_db) return;
  const existing = await _db.collection('app_settings').findOne({ _id: 'app_settings' });
  if (existing?.settings && Object.keys(existing.settings).length > 0) {
    return; // Sudah ada — jangan overwrite
  }

  const seed = {};
  for (const key of Object.keys(DEFAULTS)) {
    const envName = ENV_MAPPING[key];
    const envValue = envName ? process.env[envName] : undefined;
    if (envValue !== undefined && envValue !== '') {
      seed[key] = envValue; // simpan as-is (string), coerce nanti saat load
    }
  }

  if (Object.keys(seed).length > 0) {
    await _db.collection('app_settings').updateOne(
      { _id: 'app_settings' },
      { $set: { settings: seed, updated_at: new Date(), source: 'migrated_from_env' } },
      { upsert: true }
    );
    console.log(`[config] migrated ${Object.keys(seed).length} setting(s) from .env to DB`);
  }
}

module.exports = {
  init, invalidate, loadAll, get, save, migrateFromEnv,
  DEFAULTS, ENV_MAPPING,
};
