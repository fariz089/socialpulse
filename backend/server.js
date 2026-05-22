const express = require('express');
const cors = require('cors');
const { MongoClient, ObjectId } = require('mongodb');
const bcrypt = require('bcryptjs');
const jwt = require('jsonwebtoken');
const scheduler = require('./scheduler');
const { dispatchAlert } = require('./notifier');
const config = require('./config');

const app = express();
const PORT = process.env.PORT || 3001;
const MONGO_URI = process.env.MONGO_URI || 'mongodb://mongo:27017';
const DB_NAME = process.env.DB_NAME || 'slaytics';
const JWT_SECRET = process.env.JWT_SECRET || 'slaytics-secret-key-change-in-production-2024';

app.use(cors());
app.use(express.json({ limit: '50mb' }));

let db;
let client;

// Connect to MongoDB
async function connectDB() {
  try {
    client = new MongoClient(MONGO_URI);
    await client.connect();
    db = client.db(DB_NAME);
    console.log('Connected to MongoDB');
    
    // Create indexes
    await db.collection('posts').createIndex({ project_id: 1 });
    await db.collection('posts').createIndex({ platform: 1 });
    await db.collection('posts').createIndex({ sentiment: 1 });
    await db.collection('posts').createIndex({ post_date: 1 });
    await db.collection('posts').createIndex({ external_id: 1 });
    // Hardening: dedup di level DB. partialFilterExpression supaya post tanpa
    // external_id (legacy / news yg gak punya ID) gak ke-reject duplicate.
    await db.collection('posts').createIndex(
      { project_id: 1, platform: 1, external_id: 1 },
      { unique: true, partialFilterExpression: { external_id: { $type: 'string' } } }
    );
    await db.collection('projects').createIndex({ user_id: 1 });
    await db.collection('users').createIndex({ username: 1 }, { unique: true });
    await db.collection('users').createIndex({ email: 1 }, { unique: true });
    
    console.log('Indexes created');
  } catch (e) {
    console.error('MongoDB connection error:', e);
    process.exit(1);
  }
}

// ===== AUTH MIDDLEWARE =====
const authenticateToken = (req, res, next) => {
  const authHeader = req.headers['authorization'];
  const token = authHeader && authHeader.split(' ')[1];
  
  if (!token) {
    return res.status(401).json({ error: 'Access token required' });
  }
  
  jwt.verify(token, JWT_SECRET, (err, user) => {
    if (err) {
      return res.status(403).json({ error: 'Invalid or expired token' });
    }
    req.user = user;
    next();
  });
};

const optionalAuth = (req, res, next) => {
  const authHeader = req.headers['authorization'];
  const token = authHeader && authHeader.split(' ')[1];
  
  if (token) {
    jwt.verify(token, JWT_SECRET, (err, user) => {
      if (!err) req.user = user;
    });
  }
  next();
};

// ===== AUTH ROUTES =====
app.post('/api/auth/register', async (req, res) => {
  const { username, email, password } = req.body;
  
  if (!username || !email || !password) {
    return res.status(400).json({ error: 'Username, email, and password required' });
  }
  
  if (password.length < 6) {
    return res.status(400).json({ error: 'Password must be at least 6 characters' });
  }
  
  try {
    const existing = await db.collection('users').findOne({
      $or: [{ username }, { email }]
    });
    
    if (existing) {
      return res.status(400).json({ error: 'Username or email already exists' });
    }
    
    const hashedPassword = await bcrypt.hash(password, 10);
    const result = await db.collection('users').insertOne({
      username,
      email,
      password: hashedPassword,
      role: 'user',
      created_at: new Date()
    });
    
    const token = jwt.sign({ id: result.insertedId.toString(), username, email }, JWT_SECRET, { expiresIn: '30d' });
    
    res.json({ 
      success: true, 
      token,
      user: { id: result.insertedId.toString(), username, email }
    });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post('/api/auth/login', async (req, res) => {
  const { username, password } = req.body;
  
  if (!username || !password) {
    return res.status(400).json({ error: 'Username and password required' });
  }
  
  try {
    const user = await db.collection('users').findOne({
      $or: [{ username }, { email: username }]
    });
    
    if (!user) {
      return res.status(401).json({ error: 'Invalid credentials' });
    }
    
    const validPassword = await bcrypt.compare(password, user.password);
    if (!validPassword) {
      return res.status(401).json({ error: 'Invalid credentials' });
    }
    
    const token = jwt.sign({ 
      id: user._id.toString(), 
      username: user.username, 
      email: user.email 
    }, JWT_SECRET, { expiresIn: '30d' });
    
    res.json({ 
      success: true, 
      token,
      user: { id: user._id.toString(), username: user.username, email: user.email }
    });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get('/api/auth/me', authenticateToken, async (req, res) => {
  try {
    const user = await db.collection('users').findOne(
      { _id: new ObjectId(req.user.id) },
      { projection: { password: 0 } }
    );
    if (!user) return res.status(404).json({ error: 'User not found' });
    res.json({ ...user, id: user._id.toString() });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ===== SETTINGS =====

// ============================================================================
// IMAGE PROXY — untuk avatar Instagram (CDN IG block hot-link cross-origin)
// ============================================================================
// IG CDN (`*.fbcdn.net`, `*.cdninstagram.com`) cek Sec-Fetch-Site / Referer dan
// return 403 untuk request yang bukan dari instagram.com. Browser tidak bisa
// override Sec-Fetch-Site (forbidden header). Solusi: proxy via backend.
//
// Endpoint: GET /api/img-proxy?url=<encoded_url>
// - Cuma terima URL dari whitelist host (FB/IG CDN)
// - Forward sebagai referer instagram.com supaya CDN happy
// - Stream response (nggak buffer di memory)
// - Cache-Control 1 hari (avatar URL ada signed param expires ~24-48 jam,
//   jadi 1 hari aman, dan ngurangin load backend kalau page di-refresh)

const PROXY_ALLOWED_HOST_SUFFIXES = [
  '.fbcdn.net',
  '.cdninstagram.com',
];

app.get('/api/img-proxy', async (req, res) => {
  const target = req.query.url;
  if (!target || typeof target !== 'string') {
    return res.status(400).json({ error: 'url query param required' });
  }

  let parsed;
  try {
    parsed = new URL(target);
  } catch {
    return res.status(400).json({ error: 'invalid url' });
  }

  if (parsed.protocol !== 'https:') {
    return res.status(400).json({ error: 'only https allowed' });
  }

  const host = parsed.hostname.toLowerCase();
  const allowed = PROXY_ALLOWED_HOST_SUFFIXES.some(suf => host.endsWith(suf));
  if (!allowed) {
    return res.status(403).json({ error: 'host not allowed' });
  }

  try {
    const upstream = await fetch(parsed.toString(), {
      headers: {
        // IG CDN cek Referer/Origin. Forge dari instagram.com supaya ke-allow.
        'Referer': 'https://www.instagram.com/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
      },
      // Timeout via AbortController — IG CDN biasanya respond <2s, kasih 8s margin.
      signal: AbortSignal.timeout(8000),
    });

    if (!upstream.ok) {
      return res.status(upstream.status).json({
        error: `upstream returned ${upstream.status}`,
      });
    }

    const ctype = upstream.headers.get('content-type') || 'image/jpeg';
    if (!ctype.startsWith('image/')) {
      return res.status(415).json({ error: 'upstream not an image' });
    }

    res.set('Content-Type', ctype);
    res.set('Cache-Control', 'public, max-age=86400, immutable');
    // CORS — frontend di port lain
    res.set('Access-Control-Allow-Origin', '*');
    // Explicit override CORP. IG asli set Cross-Origin-Resource-Policy:
    // same-origin → browser block render walau request 200. Kita tidak
    // forward header upstream (di-loop tidak ada), tapi paksa nilai aman
    // di response kita supaya kalau ada middleware/CDN di depan yg sempat
    // inject CORP, kita tetap menang.
    res.set('Cross-Origin-Resource-Policy', 'cross-origin');

    // Stream body langsung ke client (tanpa buffer in-memory)
    const reader = upstream.body.getReader();
    const pump = async () => {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        res.write(Buffer.from(value));
      }
      res.end();
    };
    await pump();
  } catch (e) {
    if (!res.headersSent) {
      const isTimeout = e.name === 'TimeoutError' || e.name === 'AbortError';
      res.status(isTimeout ? 504 : 502).json({ error: e.message });
    } else {
      res.end();
    }
  }
});

// ============================================================================
// APP SETTINGS — bulk get/save + per-channel test endpoints
// Settings yang sebelumnya di .env sekarang bisa diatur lewat UI.
// ============================================================================

// GET semua settings (mask sensitive values)
app.get('/api/settings/all', optionalAuth, async (req, res) => {
  try {
    const all = await config.loadAll();
    // Mask token: default cuma boolean "configured" - aman.
    // Kalau frontend butuh visual hint (last-4), bisa request via ?show_hint=1
    // (tapi ini opt-in, bukan default).
    const masked = { ...all };
    const sensitive = ['openrouter_api_key', 'telegram_bot_token', 'discord_webhook_url'];
    const showHint = req.query.show_hint === '1';
    for (const k of sensitive) {
      const v = masked[k];
      if (v && typeof v === 'string' && v.length >= 4) {
        masked[`${k}_set`] = true;
        // Hint last-4 hanya kalau di-request explicit. Default kosong.
        masked[`${k}_preview`] = showHint ? `••••${v.slice(-4)}` : '';
      } else {
        masked[`${k}_preview`] = '';
        masked[`${k}_set`] = !!v;
      }
      masked[k] = ''; // jangan pernah kirim raw value
    }
    res.json(masked);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// PUT update bulk settings
app.put('/api/settings/all', authenticateToken, async (req, res) => {
  try {
    const partial = req.body || {};
    // Filter empty strings — supaya gak overwrite existing value dengan kosong
    // (kecuali user explicit kirim null untuk clear)
    const cleaned = {};
    for (const [k, v] of Object.entries(partial)) {
      if (v === null) {
        cleaned[k] = ''; // explicit clear
      } else if (v !== undefined && v !== '') {
        cleaned[k] = v;
      }
    }
    const updated = await config.save(cleaned);
    res.json({ ok: true, settings_updated: Object.keys(cleaned), full: { ...updated,
      // Mask di response juga
      openrouter_api_key: updated.openrouter_api_key ? '•••set•••' : '',
      telegram_bot_token: updated.telegram_bot_token ? '•••set•••' : '',
      discord_webhook_url: updated.discord_webhook_url ? '•••set•••' : '',
    }});
  } catch (e) {
    res.status(400).json({ error: e.message });
  }
});

// POST test Telegram — kirim pesan test pakai token+chat_id yang ada di DB
// Body optional: { token, chat_id } kalau mau test value yang belum di-save
app.post('/api/settings/test/telegram', authenticateToken, async (req, res) => {
  try {
    const token = req.body?.token || config.get('telegram_bot_token');
    const chatId = req.body?.chat_id || config.get('telegram_chat_id');
    if (!token || !chatId) {
      return res.status(400).json({ ok: false, error: 'token dan chat_id harus diisi' });
    }
    const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chat_id: chatId,
        text: '✓ <b>Slaytics test alert</b>\nTelegram terhubung. Bos akan terima alert di sini saat ada anomali.',
        parse_mode: 'HTML',
      }),
    });
    const data = await r.json();
    if (!data.ok) return res.status(400).json({ ok: false, error: data.description || 'Telegram API error' });
    res.json({ ok: true, message: 'Test berhasil dikirim ke Telegram' });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
});

// POST test Discord webhook
app.post('/api/settings/test/discord', authenticateToken, async (req, res) => {
  try {
    const url = req.body?.webhook || config.get('discord_webhook_url');
    if (!url) return res.status(400).json({ ok: false, error: 'webhook URL kosong' });
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: 'Slaytics Sentinel',
        embeds: [{
          title: '✓ Slaytics test alert',
          description: 'Discord terhubung. Bos akan terima alert di sini saat ada anomali.',
          color: 0x10b981,
        }],
      }),
    });
    if (!r.ok) return res.status(400).json({ ok: false, error: `HTTP ${r.status}` });
    res.json({ ok: true, message: 'Test berhasil dikirim ke Discord' });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
});

// POST test OpenRouter — verifikasi API key + model bisa dipanggil
app.post('/api/settings/test/openrouter', authenticateToken, async (req, res) => {
  try {
    const apiKey = req.body?.api_key || config.get('openrouter_api_key');
    const model = req.body?.model || config.get('openrouter_model');
    if (!apiKey) return res.status(400).json({ ok: false, error: 'API key kosong' });
    const r = await fetch('https://openrouter.ai/api/v1/chat/completions', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        model,
        messages: [{ role: 'user', content: 'ping' }],
        max_tokens: 5,
      }),
    });
    if (!r.ok) {
      const txt = await r.text();
      return res.status(400).json({ ok: false, error: `HTTP ${r.status}: ${txt.slice(0, 200)}` });
    }
    const data = await r.json();
    res.json({
      ok: true,
      message: `OpenRouter OK — model "${model}" merespon`,
      usage: data.usage,
    });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
});

// POST /api/ai/analysis — server-side proxy ke OpenRouter chat completions.
// Frontend TIDAK pernah punya akses ke API key real; key disimpan di
// settings DB / .env dan injected di sini. Frontend cukup kirim
// {systemPrompt, userPrompt} (atau {messages}); backend handle auth +
// fallback model rotation kalau rate-limited.
//
// Kenapa endpoint ini ada?
//   Sebelumnya frontend coba fetch openrouter.ai langsung dengan literal
//   string '__server_managed__' sebagai Bearer token — itu cuma flag
//   "key sudah di-set di server", bukan key real, jadi OpenRouter return
//   401 "Missing Authentication header". Sekarang frontend panggil
//   endpoint ini, dan kita yang authenticate ke OpenRouter pakai key real.
app.post('/api/ai/analysis', optionalAuth, async (req, res) => {
  try {
    const apiKey = config.get('openrouter_api_key');
    if (!apiKey) {
      return res.status(400).json({
        error: 'openrouter_not_configured',
        message: 'OpenRouter API key belum di-set. Buka Settings → AI Agent dan masukkan API key.',
      });
    }

    const {
      systemPrompt,
      userPrompt,
      messages,           // alternative: kirim messages langsung
      model,              // optional override
      temperature = 0.7,
      max_tokens = 4000,
    } = req.body || {};

    // Build messages array
    let msgArray;
    if (Array.isArray(messages) && messages.length > 0) {
      msgArray = messages;
    } else if (systemPrompt && userPrompt) {
      msgArray = [
        { role: 'system', content: String(systemPrompt) },
        { role: 'user', content: String(userPrompt) },
      ];
    } else {
      return res.status(400).json({
        error: 'bad_request',
        message: 'Body harus berisi {systemPrompt, userPrompt} ATAU {messages: [...]}',
      });
    }

    // Model fallback list — kalau model pertama 429, otomatis coba next.
    // Frontend boleh override `model` field; kalau di-set, list jadi
    // [overrideModel, ...defaults] supaya tetap ada fallback.
    const DEFAULT_MODELS = [
      config.get('openrouter_model') || 'google/gemini-2.5-flash',
      'google/gemini-2.0-flash-lite-001',
      'google/gemini-flash-1.5',
      'google/gemini-2.0-flash-001',
      'meta-llama/llama-3.1-8b-instruct:free',
      'mistralai/mistral-7b-instruct:free',
    ];
    const modelsToTry = model
      ? [model, ...DEFAULT_MODELS.filter(m => m !== model)]
      : DEFAULT_MODELS;
    // Dedup preserving order
    const seen = new Set();
    const uniqueModels = modelsToTry.filter(m => {
      if (!m || seen.has(m)) return false;
      seen.add(m);
      return true;
    });

    let lastErr = null;
    let lastStatus = null;
    let lastBody = null;

    for (const m of uniqueModels) {
      try {
        const upstream = await fetch('https://openrouter.ai/api/v1/chat/completions', {
          method: 'POST',
          headers: {
            'Authorization': `Bearer ${apiKey}`,
            'Content-Type': 'application/json',
            'HTTP-Referer': config.get('openrouter_referer') || 'http://localhost',
            'X-Title': 'Slaytics SocialPulse',
          },
          body: JSON.stringify({
            model: m,
            messages: msgArray,
            temperature,
            max_tokens,
          }),
        });

        if (!upstream.ok) {
          const txt = await upstream.text();
          lastStatus = upstream.status;
          lastBody = txt.slice(0, 500);
          // 429 / rate-limit → coba model berikutnya
          if (upstream.status === 429 ||
              txt.includes('rate-limited') ||
              txt.includes('rate_limit')) {
            console.warn(`[ai/analysis] Model ${m} rate-limited, trying next…`);
            lastErr = new Error(`HTTP ${upstream.status}: ${txt.slice(0, 200)}`);
            continue;
          }
          // Selain rate-limit, fail-fast (4xx user error / 5xx server error)
          return res.status(upstream.status).json({
            error: 'openrouter_error',
            status: upstream.status,
            message: txt.slice(0, 500),
            model_used: m,
          });
        }

        const data = await upstream.json();
        const content = data?.choices?.[0]?.message?.content || '';
        if (!content) {
          return res.status(502).json({
            error: 'empty_response',
            message: 'OpenRouter mengembalikan response tanpa content',
            model_used: m,
            raw: JSON.stringify(data).slice(0, 500),
          });
        }
        return res.json({
          ok: true,
          content,
          model_used: m,
          usage: data.usage,
        });
      } catch (e) {
        lastErr = e;
        console.warn(`[ai/analysis] Model ${m} network error:`, e.message);
        continue;
      }
    }

    // Semua model gagal
    return res.status(503).json({
      error: 'all_models_failed',
      message: lastErr?.message || 'Semua model AI sedang sibuk, coba beberapa saat lagi.',
      last_status: lastStatus,
      last_body: lastBody,
    });
  } catch (e) {
    console.error('[ai/analysis] Unhandled error:', e);
    res.status(500).json({ error: 'internal_error', message: e.message });
  }
});

app.get('/api/settings/:key', async (req, res) => {
  try {
    const setting = await db.collection('settings').findOne({ key: req.params.key });
    res.json({ value: setting?.value || null });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post('/api/settings', async (req, res) => {
  const { key, value } = req.body;
  try {
    await db.collection('settings').updateOne(
      { key },
      { $set: { key, value, updated_at: new Date() } },
      { upsert: true }
    );
    res.json({ success: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// PUT /api/settings/:key - update specific setting
app.put('/api/settings/:key', async (req, res) => {
  const { value } = req.body;
  try {
    await db.collection('settings').updateOne(
      { key: req.params.key },
      { $set: { key: req.params.key, value, updated_at: new Date() } },
      { upsert: true }
    );
    res.json({ success: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Helper function to sanitize keywords
function sanitizeKeywords(input) {
  if (!input) return '';
  if (Array.isArray(input)) {
    return input.map(k => k.toString().replace(/[""''`]/g, '').replace(/[\u200B-\u200D\uFEFF]/g, '').trim()).filter(k => k.length > 0);
  }
  return input.toString()
    .replace(/[""''`]/g, '')
    .replace(/[\u200B-\u200D\uFEFF]/g, '')
    .split(',')
    .map(k => k.trim())
    .filter(k => k.length > 0);
}

// ===== PROJECTS =====
app.get('/api/projects', optionalAuth, async (req, res) => {
  try {
    const query = req.user ? { user_id: req.user.id } : {};
    const projects = await db.collection('projects').find(query).sort({ created_at: -1 }).toArray();
    
    // Add post counts
    for (let p of projects) {
      const count = await db.collection('posts').countDocuments({ project_id: p._id.toString() });
      p.id = p._id.toString();
      p.total_mentions = count;
      delete p._id;
    }
    
    res.json(projects);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post('/api/projects', optionalAuth, async (req, res) => {
  const { name, keywords, platforms, language, color, excluded_keywords } = req.body;
  if (!name || !keywords) return res.status(400).json({ error: 'Name and keywords required' });
  
  // Sanitize keywords before saving
  const cleanKeywords = sanitizeKeywords(keywords);
  console.log('[Project Create] Keywords sanitized:', { original: keywords, cleaned: cleanKeywords });
  
  try {
    const result = await db.collection('projects').insertOne({
      user_id: req.user?.id || null,
      name,
      keywords: JSON.stringify(cleanKeywords),
      language: language || 'id',
      excluded_keywords: JSON.stringify(excluded_keywords || []),
      platforms: JSON.stringify(platforms || ['tiktok', 'twitter', 'instagram', 'news']),
      color: color || '#6366f1',
      created_at: new Date(),
      updated_at: new Date()
    });
    
    res.json({ id: result.insertedId.toString(), name });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.put('/api/projects/:id', optionalAuth, async (req, res) => {
  const { name, keywords, platforms, language, color, excluded_keywords } = req.body;
  try {
    // Ownership check — kalau project punya user_id, hanya owner yg boleh edit
    const proj = await db.collection('projects').findOne({ _id: new ObjectId(req.params.id) });
    if (!proj) return res.status(404).json({ error: 'Project not found' });
    if (proj.user_id && proj.user_id !== req.user?.id) {
      return res.status(403).json({ error: 'Forbidden — bukan owner project ini' });
    }

    const updateData = { updated_at: new Date() };
    if (name) updateData.name = name;
    if (keywords) {
      // Sanitize keywords before saving
      const cleanKeywords = sanitizeKeywords(keywords);
      console.log('[Project Update] Keywords sanitized:', { original: keywords, cleaned: cleanKeywords });
      updateData.keywords = JSON.stringify(cleanKeywords);
    }
    if (platforms) updateData.platforms = JSON.stringify(platforms);
    if (language !== undefined) updateData.language = language;
    if (color) updateData.color = color;
    if (excluded_keywords) updateData.excluded_keywords = JSON.stringify(excluded_keywords);
    
    await db.collection('projects').updateOne(
      { _id: new ObjectId(req.params.id) },
      { $set: updateData }
    );
    res.json({ success: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.delete('/api/projects/:id', optionalAuth, async (req, res) => {
  try {
    // Ownership check
    const proj = await db.collection('projects').findOne({ _id: new ObjectId(req.params.id) });
    if (!proj) return res.status(404).json({ error: 'Project not found' });
    if (proj.user_id && proj.user_id !== req.user?.id) {
      return res.status(403).json({ error: 'Forbidden — bukan owner project ini' });
    }

    await db.collection('posts').deleteMany({ project_id: req.params.id });
    await db.collection('scrape_sessions').deleteMany({ project_id: req.params.id });
    await db.collection('scrape_checkpoints').deleteMany({ project_id: req.params.id });
    await db.collection('projects').deleteOne({ _id: new ObjectId(req.params.id) });
    res.json({ success: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ===== SCRAPE SESSIONS =====
app.post('/api/sessions', async (req, res) => {
  const { project_id, platforms, date_from, date_to, max_results } = req.body;
  try {
    const result = await db.collection('scrape_sessions').insertOne({
      project_id,
      platforms: JSON.stringify(platforms),
      date_from,
      date_to,
      max_results: max_results || 10,
      total_results: 0,
      status: 'running',
      created_at: new Date()
    });
    res.json({ id: result.insertedId.toString() });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.patch('/api/sessions/:id', async (req, res) => {
  const { status, total_results } = req.body;
  try {
    const updateData = {};
    if (status) updateData.status = status;
    if (total_results !== undefined) updateData.total_results = total_results;
    
    await db.collection('scrape_sessions').updateOne(
      { _id: new ObjectId(req.params.id) },
      { $set: updateData }
    );
    res.json({ success: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// PUT /api/sessions/:id - update session (alias for PATCH)
app.put('/api/sessions/:id', async (req, res) => {
  const { status, total_results } = req.body;
  try {
    const updateData = {};
    if (status) updateData.status = status;
    if (total_results !== undefined) updateData.total_results = total_results;
    
    await db.collection('scrape_sessions').updateOne(
      { _id: new ObjectId(req.params.id) },
      { $set: updateData }
    );
    res.json({ success: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ===== POSTS =====
app.post('/api/posts', async (req, res) => {
  const postsData = Array.isArray(req.body) ? req.body : [req.body];
  try {
    let inserted = 0;
    for (const p of postsData) {
      // Check for duplicate
      if (p.external_id) {
        const existing = await db.collection('posts').findOne({
          project_id: p.project_id,
          external_id: p.external_id
        });
        if (existing) continue;
      }
      
      await db.collection('posts').insertOne({
        ...p,
        views: p.views || 0,
        likes: p.likes || 0,
        shares: p.shares || 0,
        comments: p.comments || 0,
        sentiment: p.sentiment || 'neutral',
        influence_score: p.influence_score || 0,
        cities: p.cities || '[]',
        hashtags: p.hashtags || '[]',
        post_date: p.post_date ? new Date(p.post_date) : null,
        created_at: new Date()
      });
      inserted++;
    }
    
    // Update session count if session_id provided
    if (postsData[0]?.session_id) {
      await db.collection('scrape_sessions').updateOne(
        { _id: new ObjectId(postsData[0].session_id) },
        { $inc: { total_results: inserted } }
      );
    }
    
    res.json({ inserted });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Bulk insert posts (from frontend scraping)
app.post('/api/posts/bulk', async (req, res) => {
  const { session_id, project_id, posts } = req.body;
  
  if (!posts || !Array.isArray(posts)) {
    return res.status(400).json({ error: 'Posts array required' });
  }
  
  try {
    let inserted = 0;
    for (const p of posts) {
      // Check for duplicate
      if (p.external_id) {
        const existing = await db.collection('posts').findOne({
          project_id: project_id,
          external_id: p.external_id
        });
        if (existing) continue;
      }
      
      await db.collection('posts').insertOne({
        project_id: project_id,
        external_id: p.external_id,
        platform: p.platform,
        keyword_matched: p.keyword_matched,
        author: p.author,
        handle: p.handle,
        avatar: p.avatar,
        content: p.content,
        views: p.views || 0,
        likes: p.likes || 0,
        shares: p.shares || 0,
        comments: p.comments || 0,
        sentiment: p.sentiment || 'neutral',
        cities: typeof p.cities === 'string' ? p.cities : JSON.stringify(p.cities || []),
        hashtags: typeof p.hashtags === 'string' ? p.hashtags : JSON.stringify(p.hashtags || []),
        source_name: p.source_name,
        url: p.url,
        post_date: p.post_date ? new Date(p.post_date) : null,
        created_at: new Date()
      });
      inserted++;
    }
    
    // Update session count
    if (session_id) {
      try {
        await db.collection('scrape_sessions').updateOne(
          { _id: new ObjectId(session_id) },
          { $inc: { total_results: inserted } }
        );
      } catch (e) {
        console.log('Session update error:', e.message);
      }
    }
    
    console.log(`Bulk insert: ${inserted}/${posts.length} posts saved for project ${project_id}`);
    res.json({ inserted, total: posts.length });
  } catch (e) {
    console.error('Bulk insert error:', e);
    res.status(500).json({ error: e.message });
  }
});

app.get('/api/posts', async (req, res) => {
  const { project_id, platform, sentiment, date_from, date_to, search, page = 1, limit = 10000, sort = 'post_date' } = req.query;
  
  try {
    const query = {};
    if (project_id) query.project_id = project_id;
    if (platform) query.platform = platform;
    if (sentiment) query.sentiment = sentiment;
    if (date_from) query.post_date = { $gte: new Date(date_from) };
    if (date_to) {
      query.post_date = query.post_date || {};
      query.post_date.$lte = new Date(date_to + 'T23:59:59');
    }
    if (search) {
      query.$or = [
        { content: { $regex: search, $options: 'i' } },
        { author: { $regex: search, $options: 'i' } }
      ];
    }
    
    const sortField = sort === 'views' ? { views: -1 } : { post_date: -1 };
    const skip = (parseInt(page) - 1) * parseInt(limit);
    
    const posts = await db.collection('posts')
      .find(query)
      .sort(sortField)
      .skip(skip)
      .limit(parseInt(limit))
      .toArray();
    
    const total = await db.collection('posts').countDocuments(query);
    
    // Transform _id to id
    const transformedPosts = posts.map(p => ({
      ...p,
      id: p._id.toString(),
      _id: undefined
    }));
    
    res.json({ posts: transformedPosts, total, page: parseInt(page), limit: parseInt(limit) });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ===== EXPORT JSON =====
app.get('/api/export/:project_id', async (req, res) => {
  const { date_from, date_to, format } = req.query;
  
  try {
    const project = await db.collection('projects').findOne({ _id: new ObjectId(req.params.project_id) });
    if (!project) return res.status(404).json({ error: 'Project not found' });
    
    const query = { project_id: req.params.project_id };
    if (date_from) query.post_date = { $gte: new Date(date_from) };
    if (date_to) {
      query.post_date = query.post_date || {};
      query.post_date.$lte = new Date(date_to + 'T23:59:59');
    }
    
    const posts = await db.collection('posts').find(query).sort({ post_date: -1 }).toArray();
    
    // Get stats
    const stats = {
      total_mentions: posts.length,
      total_reach: posts.reduce((s, p) => s + (p.views || 0), 0),
      total_likes: posts.reduce((s, p) => s + (p.likes || 0), 0),
      total_shares: posts.reduce((s, p) => s + (p.shares || 0), 0),
      total_comments: posts.reduce((s, p) => s + (p.comments || 0), 0),
      sentiment: {
        positive: posts.filter(p => p.sentiment === 'positive').length,
        neutral: posts.filter(p => p.sentiment === 'neutral').length,
        negative: posts.filter(p => p.sentiment === 'negative').length
      },
      platforms: {}
    };
    
    // Count by platform
    posts.forEach(p => {
      stats.platforms[p.platform] = (stats.platforms[p.platform] || 0) + 1;
    });
    
    // Parse project keywords
    let keywords = [];
    try { keywords = JSON.parse(project.keywords); } catch {}
    
    const exportData = {
      project: {
        id: project._id.toString(),
        name: project.name,
        keywords,
        language: project.language,
        color: project.color
      },
      period: {
        from: date_from || 'all',
        to: date_to || 'all',
        exported_at: new Date().toISOString()
      },
      summary: stats,
      posts: posts.map(p => ({
        id: p._id.toString(),
        platform: p.platform,
        author: p.author,
        handle: p.handle,
        content: p.content,
        url: p.url,
        views: p.views,
        likes: p.likes,
        shares: p.shares,
        comments: p.comments,
        sentiment: p.sentiment,
        post_date: p.post_date,
        source_name: p.source_name,
        hashtags: p.hashtags,
        cities: p.cities
      }))
    };
    
    if (format === 'csv') {
      // CSV export
      const csvRows = [
        ['Platform', 'Author', 'Handle', 'Content', 'URL', 'Views', 'Likes', 'Shares', 'Comments', 'Sentiment', 'Date'].join(',')
      ];
      posts.forEach(p => {
        csvRows.push([
          p.platform,
          `"${(p.author || '').replace(/"/g, '""')}"`,
          `"${(p.handle || '').replace(/"/g, '""')}"`,
          `"${(p.content || '').replace(/"/g, '""').substring(0, 500)}"`,
          p.url || '',
          p.views || 0,
          p.likes || 0,
          p.shares || 0,
          p.comments || 0,
          p.sentiment,
          p.post_date ? new Date(p.post_date).toISOString() : ''
        ].join(','));
      });
      
      res.setHeader('Content-Type', 'text/csv');
      res.setHeader('Content-Disposition', `attachment; filename="${project.name}_export.csv"`);
      return res.send(csvRows.join('\n'));
    }
    
    // JSON export (default)
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Content-Disposition', `attachment; filename="${project.name}_export.json"`);
    res.json(exportData);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ===== STATS =====
app.get('/api/stats/:project_id', async (req, res) => {
  const { date_from, date_to, platform } = req.query;
  
  try {
    const query = { project_id: req.params.project_id };
    if (date_from) query.post_date = { $gte: new Date(date_from) };
    if (date_to) {
      query.post_date = query.post_date || {};
      query.post_date.$lte = new Date(date_to + 'T23:59:59');
    }
    if (platform) query.platform = platform;
    
    const posts = await db.collection('posts').find(query).toArray();
    
    // Calculate totals
    const totals = {
      total: posts.length,
      total_views: posts.reduce((s, p) => s + (p.views || 0), 0),
      total_likes: posts.reduce((s, p) => s + (p.likes || 0), 0),
      total_shares: posts.reduce((s, p) => s + (p.shares || 0), 0),
      total_comments: posts.reduce((s, p) => s + (p.comments || 0), 0),
      positive: posts.filter(p => p.sentiment === 'positive').length,
      neutral: posts.filter(p => p.sentiment === 'neutral').length,
      negative: posts.filter(p => p.sentiment === 'negative').length
    };
    
    // By platform
    const platformMap = {};
    posts.forEach(p => {
      if (!platformMap[p.platform]) platformMap[p.platform] = { count: 0, views: 0 };
      platformMap[p.platform].count++;
      platformMap[p.platform].views += p.views || 0;
    });
    const byPlatform = Object.entries(platformMap)
      .map(([platform, data]) => ({ platform, ...data }))
      .sort((a, b) => b.count - a.count);
    
    // By date
    const dateMap = {};
    posts.forEach(p => {
      if (!p.post_date) return;
      const date = new Date(p.post_date).toISOString().split('T')[0];
      if (!dateMap[date]) dateMap[date] = {};
      if (!dateMap[date][p.platform]) dateMap[date][p.platform] = { count: 0, views: 0 };
      dateMap[date][p.platform].count++;
      dateMap[date][p.platform].views += p.views || 0;
    });
    const byDate = [];
    Object.entries(dateMap).forEach(([date, platforms]) => {
      Object.entries(platforms).forEach(([platform, data]) => {
        byDate.push({ date, platform, count: data.count, views: data.views });
      });
    });
    byDate.sort((a, b) => a.date.localeCompare(b.date));
    
    // Sentiment by date
    const sentDateMap = {};
    posts.forEach(p => {
      if (!p.post_date) return;
      const date = new Date(p.post_date).toISOString().split('T')[0];
      if (!sentDateMap[date]) sentDateMap[date] = {};
      sentDateMap[date][p.sentiment] = (sentDateMap[date][p.sentiment] || 0) + 1;
    });
    const sentimentByDate = [];
    Object.entries(sentDateMap).forEach(([date, sentiments]) => {
      Object.entries(sentiments).forEach(([sentiment, count]) => {
        sentimentByDate.push({ date, sentiment, count });
      });
    });
    sentimentByDate.sort((a, b) => a.date.localeCompare(b.date));
    
    // Top authors
    const authorMap = {};
    posts.forEach(p => {
      if (!p.author) return;
      if (!authorMap[p.author]) authorMap[p.author] = { platform: p.platform, post_count: 0, views: 0, positive: 0, negative: 0 };
      authorMap[p.author].post_count++;
      authorMap[p.author].views += p.views || 0;
      if (p.sentiment === 'positive') authorMap[p.author].positive++;
      if (p.sentiment === 'negative') authorMap[p.author].negative++;
    });
    const topAuthors = Object.entries(authorMap)
      .map(([author, data]) => ({ author, ...data }))
      .sort((a, b) => b.views - a.views)
      .slice(0, 20);
    
    // Hourly
    const hourMap = {};
    posts.forEach(p => {
      if (!p.post_date) return;
      const hour = new Date(p.post_date).getHours();
      hourMap[hour] = (hourMap[hour] || 0) + 1;
    });
    const hourly = Object.entries(hourMap).map(([hour, count]) => ({ hour: parseInt(hour), count }));
    hourly.sort((a, b) => a.hour - b.hour);
    
    // Sources
    const sourceMap = {};
    posts.forEach(p => {
      if (!p.source_name) return;
      sourceMap[p.source_name] = (sourceMap[p.source_name] || 0) + 1;
    });
    const sources = Object.entries(sourceMap)
      .map(([source_name, count]) => ({ source_name, count }))
      .sort((a, b) => b.count - a.count)
      .slice(0, 15);
    
    // Hashtags
    const hashMap = {};
    posts.forEach(p => {
      try {
        const tags = typeof p.hashtags === 'string' ? JSON.parse(p.hashtags) : (p.hashtags || []);
        tags.forEach(h => {
          const tag = h.toLowerCase();
          hashMap[tag] = (hashMap[tag] || 0) + 1;
        });
      } catch {}
    });
    
    res.json({ totals, byPlatform, byDate, sentimentByDate, topAuthors, hourly, topHashtags: hashMap, sources });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ===== COMPARISON =====
app.post('/api/compare', async (req, res) => {
  const { project_ids, date_from, date_to } = req.body;
  if (!project_ids || project_ids.length < 2) return res.status(400).json({ error: 'Need >=2 projects' });
  
  try {
    const results = [];
    
    for (const pid of project_ids) {
      const project = await db.collection('projects').findOne({ _id: new ObjectId(pid) });
      if (!project) continue;
      
      const query = { project_id: pid };
      if (date_from) query.post_date = { $gte: new Date(date_from) };
      if (date_to) {
        query.post_date = query.post_date || {};
        query.post_date.$lte = new Date(date_to + 'T23:59:59');
      }
      
      const posts = await db.collection('posts').find(query).toArray();
      
      let keywords = [];
      try { keywords = JSON.parse(project.keywords); } catch {}
      
      const totals = {
        mentions: posts.length,
        reach: posts.reduce((s, p) => s + (p.views || 0), 0),
        likes: posts.reduce((s, p) => s + (p.likes || 0), 0),
        shares: posts.reduce((s, p) => s + (p.shares || 0), 0),
        positive: posts.filter(p => p.sentiment === 'positive').length,
        negative: posts.filter(p => p.sentiment === 'negative').length,
        neutral: posts.filter(p => p.sentiment === 'neutral').length
      };
      
      const presenceScore = Math.min(100, Math.round((totals.mentions * 0.3 + totals.reach * 0.00001 + totals.likes * 0.001 + (totals.positive / (totals.mentions || 1)) * 30)));
      
      // By platform
      const platformMap = {};
      posts.forEach(p => {
        platformMap[p.platform] = (platformMap[p.platform] || 0) + 1;
      });
      const byPlatform = Object.entries(platformMap).map(([platform, count]) => ({ platform, count }));
      
      // By date
      const dateMap = {};
      posts.forEach(p => {
        if (!p.post_date) return;
        const date = new Date(p.post_date).toISOString().split('T')[0];
        dateMap[date] = (dateMap[date] || 0) + 1;
      });
      const byDate = Object.entries(dateMap).map(([date, count]) => ({ date, count })).sort((a, b) => a.date.localeCompare(b.date));
      
      // Sentiment by date
      const sentDateMap = {};
      posts.forEach(p => {
        if (!p.post_date) return;
        const date = new Date(p.post_date).toISOString().split('T')[0];
        if (!sentDateMap[date]) sentDateMap[date] = {};
        sentDateMap[date][p.sentiment] = (sentDateMap[date][p.sentiment] || 0) + 1;
      });
      const sentimentByDate = [];
      Object.entries(sentDateMap).forEach(([date, sentiments]) => {
        Object.entries(sentiments).forEach(([sentiment, count]) => {
          sentimentByDate.push({ date, sentiment, count });
        });
      });
      
      results.push({
        project: { id: project._id.toString(), name: project.name, keywords, color: project.color },
        totals: { ...totals, presenceScore },
        byPlatform,
        byDate,
        sentimentByDate
      });
    }
    
    const totalMentions = results.reduce((s, r) => s + r.totals.mentions, 0) || 1;
    results.forEach(r => {
      r.totals.shareOfVoice = Math.round((r.totals.mentions / totalMentions) * 100);
    });
    
    res.json({ projects: results, period: { date_from, date_to } });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ===== CHECKPOINTS =====
app.get('/api/checkpoints/:project_id', async (req, res) => {
  try {
    const checkpoints = await db.collection('scrape_checkpoints')
      .find({ project_id: req.params.project_id })
      .sort({ platform: 1, keyword: 1 })
      .toArray();
    res.json(checkpoints.map(c => ({ ...c, id: c._id.toString() })));
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get('/api/checkpoints/:project_id/summary', async (req, res) => {
  try {
    const checkpoints = await db.collection('scrape_checkpoints')
      .find({ project_id: req.params.project_id })
      .toArray();
    
    const byPlatform = {};
    checkpoints.forEach(c => {
      if (!byPlatform[c.platform]) {
        byPlatform[c.platform] = { platform: c.platform, last_date: c.last_scraped_date, last_at: c.last_scraped_at, total_posts: 0 };
      }
      if (c.last_scraped_date > byPlatform[c.platform].last_date) {
        byPlatform[c.platform].last_date = c.last_scraped_date;
      }
      if (c.last_scraped_at > byPlatform[c.platform].last_at) {
        byPlatform[c.platform].last_at = c.last_scraped_at;
      }
      byPlatform[c.platform].total_posts += c.total_posts_scraped || 0;
    });
    
    let latest = { last_date: null, last_at: null };
    checkpoints.forEach(c => {
      if (!latest.last_date || c.last_scraped_date > latest.last_date) {
        latest.last_date = c.last_scraped_date;
      }
      if (!latest.last_at || c.last_scraped_at > latest.last_at) {
        latest.last_at = c.last_scraped_at;
      }
    });
    
    res.json({ byPlatform: Object.values(byPlatform), latest });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post('/api/checkpoints', async (req, res) => {
  const { project_id, platform, keyword, last_scraped_date, posts_count } = req.body;
  try {
    await db.collection('scrape_checkpoints').updateOne(
      { project_id, platform, keyword },
      {
        $set: {
          project_id,
          platform,
          keyword,
          last_scraped_date,
          last_scraped_at: new Date()
        },
        $inc: { total_posts_scraped: posts_count || 0 }
      },
      { upsert: true }
    );
    res.json({ success: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get('/api/checkpoints/:project_id/gap', async (req, res) => {
  const { platform, keyword } = req.query;
  try {
    const query = { project_id: req.params.project_id };
    if (platform) query.platform = platform;
    if (keyword) query.keyword = keyword;
    
    const checkpoint = await db.collection('scrape_checkpoints')
      .findOne(query, { sort: { last_scraped_date: -1 } });
    
    const today = new Date().toISOString().split('T')[0];
    
    if (!checkpoint) {
      res.json({ last_date: null, today, gap_days: null, needs_scrape: true, is_first_scrape: true, already_scraped_today: false });
    } else {
      const lastDate = new Date(checkpoint.last_scraped_date);
      const todayDate = new Date(today);
      const gapDays = Math.floor((todayDate - lastDate) / (1000 * 60 * 60 * 24));
      // FIX: always allow retry — let user decide. Dedup logic in /api/posts/bulk handles duplicates.
      // Suggested range: when same-day retry, scan from last_date itself (not last_date+1) so we re-cover today.
      const suggestedFrom = gapDays > 0
        ? new Date(lastDate.getTime() + 86400000).toISOString().split('T')[0]
        : checkpoint.last_scraped_date;
      res.json({
        last_date: checkpoint.last_scraped_date,
        today,
        gap_days: gapDays,
        needs_scrape: true, // always true — user decides via UI
        is_first_scrape: false,
        already_scraped_today: gapDays === 0,
        suggested_from: suggestedFrom
      });
    }
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ===== ANDROID SCRAPER POOL (LOAD-BALANCED) =====
// Multi-HP setup: user list URLs di Settings (comma-separated).
// Backend pilih HP yang sehat, round-robin antar request, auto-failover kalau
// 1 HP gagal/timeout — coba HP berikutnya sebelum kasih up.
//
// State: di-track in-memory (tidak persist) — round-robin index per platform.

const _scraperPoolState = {
  // platform -> last index used (untuk round-robin start point)
  rrIndex: {}
};

// Helper: ambil daftar URL dari settings, normalisasi (trim trailing slash, dedup)
async function getScraperUrls() {
  // 1. Coba dari MongoDB settings
  try {
    const setting = await db.collection('settings').findOne({ key: 'android_scraper_urls' });
    if (setting?.value) {
      const urls = [...new Set(
        setting.value.split(',').map(u => u.trim().replace(/\/$/, '')).filter(Boolean)
      )];
      if (urls.length > 0) return urls;
    }
  } catch (e) {
    console.error('[Scraper Pool] settings load failed:', e.message);
  }

  // 2. Fallback ke env PC_SCRAPER_URL — service internal docker network
  const fallback = (process.env.PC_SCRAPER_URL || '').replace(/\/$/, '');
  if (fallback) return [fallback];

  return [];
}

// Helper: panggil 1 HP dengan timeout
async function callScraper(url, payload, timeoutMs = 120000) {
  // Per-platform timeout override.
  //
  // Timeout chain (penting urutannya):
  //   Backend (here)   < nginx proxy_read_timeout (600s di frontend/nginx.conf)
  //   nginx 600s       < frontend AbortController (660s di index.html)
  //
  // Backend HARUS timeout duluan supaya error message yg sampai ke user
  // datang dari backend (yang punya context: nama HP yg gagal, attempt
  // number, dll), bukan dari nginx 504 generic page.
  //
  // Facebook scraper multi-pass (3-4 variants × 2 endpoints = 6-8 passes,
  // masing-masing 30-40s) bisa 3-5 menit. Buffer extra untuk slow network.
  const PLATFORM_TIMEOUTS = {
    facebook:  540000,  // 9 menit — multi-pass + buffer (was 6 min, sering kena timeout)
    instagram: 240000,  // 4 menit — single-pass scroll
    twitter:   240000,  // 4 menit — single-pass scroll
    tiktok:    150000,  // 2.5 menit
    youtube:   240000,  // 4 menit — full extract per-video lebih lambat dari flat
                       //           (was 90s di flat mode, tapi flat gak kasih timestamp)
    threads:   240000,  // 4 menit — single-pass scroll, mirror Twitter timing
    news:       60000,  // 1 menit — RSS
  };
  const effectiveTimeout = PLATFORM_TIMEOUTS[payload?.platform] || timeoutMs;
  
  const fetch = (await import('node-fetch')).default;
  const controller = new AbortController();
  const t = setTimeout(() => controller.abort(), effectiveTimeout);
  try {
    const response = await fetch(`${url}/scrape`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: controller.signal
    });
    clearTimeout(t);
    if (!response.ok) {
      const errText = await response.text();
      throw new Error(`HTTP ${response.status}: ${errText.slice(0, 200)}`);
    }
    return await response.json();
  } catch (e) {
    clearTimeout(t);
    // Specific message untuk timeout (better UX than "AbortError")
    if (e.name === 'AbortError') {
      throw new Error(
        `Scraper ${payload?.platform || 'unknown'} timeout setelah ${Math.round(effectiveTimeout/1000)}s ` +
        `at ${url} — multi-pass scraping mungkin masih jalan di pc-scraper, cek log container.`
      );
    }
    throw e;
  }
}

// ===== UNIFIED SCRAPE ENDPOINT =====
// Frontend hanya tahu endpoint ini. Backend yang load-balance ke HP-HP.
//
// Body: {
//   platform: 'instagram'|'tiktok'|'facebook'|'youtube'|'news'|'twitter',
//   keyword: '...',
//   max_results: 30,
//   mode?: 'comments',     // YouTube only
//   video_url?: '...',     // YouTube comments mode
//   sites?: [...]          // News only (optional filter)
// }
//
// Strategy:
//   1. Load URLs dari settings
//   2. Round-robin start point (supaya beban merata)
//   3. Coba HP[i], kalau gagal coba HP[i+1], dst sampai semua exhausted
//   4. Return hasil pertama yang sukses (atau error agregat kalau semua gagal)
// Core scrape logic — REUSABLE oleh scrapeHandler dan agent.js
// Throws error kalau semua HP fail. Return shape: { posts: [...], _scraper_url, ...meta }
async function scrapeViaScraperPool(platform, keyword, max_results = 30, extraOpts = {}) {
  if (!platform) throw new Error('platform required');

  const urls = await getScraperUrls();
  if (urls.length === 0) {
    const err = new Error('no_scrapers_configured: Tidak ada Android scraper URL di Settings.');
    err.code = 'no_scrapers_configured';
    err.statusHint = 503;
    throw err;
  }

  // Round-robin: setiap platform punya pointer sendiri supaya beban merata
  const startIdx = (_scraperPoolState.rrIndex[platform] || 0) % urls.length;
  _scraperPoolState.rrIndex[platform] = (startIdx + 1) % urls.length;

  const tryOrder = [];
  for (let i = 0; i < urls.length; i++) {
    tryOrder.push(urls[(startIdx + i) % urls.length]);
  }

  const payload = { platform, keyword, max_results: max_results || 30 };
  if (extraOpts.mode) payload.mode = extraOpts.mode;
  if (extraOpts.video_url) payload.video_url = extraOpts.video_url;
  if (extraOpts.sites) payload.sites = extraOpts.sites;

  const errors = [];

  for (const url of tryOrder) {
    try {
      console.log(`[Scrape] ${platform}/${keyword} → ${url}`);
      const data = await callScraper(url, payload);

      const count = data?.posts?.length || 0;
      console.log(`[Scrape] ${platform} → ${url}: ${count} items`);

      if (count === 0 && tryOrder.length > 1) {
        errors.push({ url, error: 'returned 0 posts' });
        continue;
      }

      data._scraper_url = url;
      return data;
    } catch (e) {
      console.warn(`[Scrape] ${url} failed: ${e.message}`);
      errors.push({ url, error: e.message.slice(0, 200) });
    }
  }

  // Semua HP gagal — bukan error fatal kalau cuma return 0 posts
  // Return empty result with attempts info supaya caller bisa decide
  const result = {
    error: 'all_scrapers_failed',
    platform,
    keyword,
    attempts: errors,
    posts: [],
  };
  return result;
}

async function scrapeHandler(req, res) {
  const { platform, keyword, max_results, mode, video_url, sites } = req.body;

  try {
    const data = await scrapeViaScraperPool(platform, keyword, max_results, { mode, video_url, sites });
    if (data.error === 'all_scrapers_failed') {
      return res.status(502).json(data);
    }
    return res.json(data);
  } catch (e) {
    if (e.code === 'no_scrapers_configured') {
      return res.status(503).json({
        error: 'no_scrapers_configured',
        message: 'Tidak ada Android scraper URL di Settings. Tambahkan minimal 1 URL.',
        posts: [],
      });
    }
    return res.status(400).json({ error: e.message });
  }
}

app.post('/api/scrape', scrapeHandler);
app._scrapeHandler = scrapeHandler;
app._scrapeViaScraperPool = scrapeViaScraperPool;

// ===== HEALTH CHECK SEMUA HP =====
// GET version: pakai URL dari settings otomatis.
// POST version: client kasih URL custom (untuk Settings → Test Connection).
app.get('/api/scrapers/health', async (req, res) => {
  const urls = await getScraperUrls();
  if (urls.length === 0) {
    return res.json({ scrapers: [], message: 'No URLs configured' });
  }
  res.json({ scrapers: await checkAllScrapers(urls) });
});

async function scrapeHealthHandler(req, res) {
  const { scraper_urls } = req.body || {};
  if (!Array.isArray(scraper_urls)) {
    return res.status(400).json({ error: 'scraper_urls array required' });
  }
  const cleaned = scraper_urls.map(u => u.trim().replace(/\/$/, '')).filter(Boolean);
  res.json({ scrapers: await checkAllScrapers(cleaned) });
}

app.post('/api/scrapers/health', scrapeHealthHandler);
app._scrapeHealthHandler = scrapeHealthHandler;

// Helper: ping /health di setiap HP, return status array
async function checkAllScrapers(urls) {
  const fetch = (await import('node-fetch')).default;
  return await Promise.all(urls.map(async (url) => {
    try {
      const controller = new AbortController();
      const t = setTimeout(() => controller.abort(), 5000);
      const r = await fetch(`${url}/health`, { signal: controller.signal });
      clearTimeout(t);
      const data = await r.json().catch(() => ({}));
      return { url, online: r.ok, ...data };
    } catch (e) {
      return { url, online: false, error: e.message };
    }
  }));
}

// ===== ACCOUNT MANAGEMENT (Fase 2) =====
// Proxy ke android-scraper(s) & tiktok-pc, untuk UI manage cookie/login per platform.
//
// Routing rules:
//   - platform=tiktok       → SELALU ke tiktok-pc service (Playwright-based)
//   - platform lainnya      → ke android-scraper pertama dari pool (atau target eksplisit)
//
// TikTok-PC URL & API key dibaca dari env (compose injects). Default match docker-compose:
//   TIKTOK_PC_URL=http://tiktok-pc:5006
//   TIKTOK_PC_API_KEY="" (kosong = no auth header)
const TIKTOK_PC_URL = (process.env.TIKTOK_PC_URL || 'http://tiktok-pc:5006').replace(/\/$/, '');
const TIKTOK_PC_API_KEY = process.env.TIKTOK_PC_API_KEY || '';

function tiktokPcHeaders() {
  const h = { 'Content-Type': 'application/json' };
  if (TIKTOK_PC_API_KEY) h['Authorization'] = `Bearer ${TIKTOK_PC_API_KEY}`;
  return h;
}

// Fetch dengan timeout — match pattern lain di file ini (dynamic import node-fetch)
async function fetchWithTimeout(url, options = {}, timeoutMs = 10000) {
  const fetch = (await import('node-fetch')).default;
  const controller = new AbortController();
  const t = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const r = await fetch(url, { ...options, signal: controller.signal });
    clearTimeout(t);
    return r;
  } catch (e) {
    clearTimeout(t);
    throw e;
  }
}

// GET /api/accounts — list akun dari semua sumber, di-merge per platform
// Response: { accounts: { instagram: [...], tiktok: [...], ... }, errors: [...], sources: { android: [...], tiktok_pc: '...' } }
app.get('/api/accounts', optionalAuth, async (req, res) => {
  const result = { instagram: [], tiktok: [], facebook: [], youtube: [], twitter: [], threads: [] };
  const errors = [];
  const sources = { android: [], tiktok_pc: TIKTOK_PC_URL };

  // 1) Dari android-scraper(s) — semua platform kecuali tiktok yang akan di-override
  const urls = await getScraperUrls();
  sources.android = urls;
  for (const url of urls) {
    try {
      const r = await fetchWithTimeout(`${url}/accounts`, {}, 10000);
      const data = await r.json();
      for (const [platform, accounts] of Object.entries(data.accounts || {})) {
        if (result[platform] && Array.isArray(accounts)) {
          result[platform].push(...accounts.map(a => ({ ...a, _source: url, _source_type: 'android' })));
        }
      }
    } catch (e) {
      errors.push({ source: url, source_type: 'android', error: e.message });
    }
  }

  // 2) Dari tiktok-pc — override TikTok kalau ada (PC service authoritative untuk TikTok)
  try {
    const r = await fetchWithTimeout(`${TIKTOK_PC_URL}/accounts`, { headers: tiktokPcHeaders() }, 10000);
    const data = await r.json();
    if (Array.isArray(data.accounts?.tiktok)) {
      result.tiktok = data.accounts.tiktok.map(a => ({ ...a, _source: TIKTOK_PC_URL, _source_type: 'tiktok-pc' }));
    }
  } catch (e) {
    errors.push({ source: TIKTOK_PC_URL, source_type: 'tiktok-pc', error: e.message });
  }

  res.json({ accounts: result, errors, sources });
});

// POST /api/accounts — add akun, route ke target yang benar berdasarkan platform
// Body: { platform, username, password, verification_code?, target? }
//   - target: optional URL android-scraper spesifik (kalau user pilih HP mana). Default: HP pertama dari pool.
app.post('/api/accounts', optionalAuth, async (req, res) => {
  const { platform, username, password, verification_code, target } = req.body || {};

  if (!platform || !username) {
    return res.status(400).json({ error: 'platform & username required' });
  }
  if (!result_platforms_supported(platform)) {
    return res.status(400).json({ error: `unsupported platform: ${platform}` });
  }

  // TikTok → SELALU ke PC service
  if (platform === 'tiktok') {
    try {
      const r = await fetchWithTimeout(`${TIKTOK_PC_URL}/accounts`, {
        method: 'POST',
        headers: tiktokPcHeaders(),
        body: JSON.stringify({ platform, username, password }),
      }, 30000);
      const data = await r.json().catch(() => ({}));
      return res.status(r.status).json(data);
    } catch (e) {
      return res.status(502).json({ error: 'tiktok-pc unreachable', detail: e.message });
    }
  }

  // Platform lain → android-scraper
  const urls = await getScraperUrls();
  if (urls.length === 0) {
    return res.status(503).json({ error: 'no_android_scrapers_configured', detail: 'Set Android Scraper URLs di Settings dulu' });
  }
  const targetUrl = (target && urls.includes(target.trim().replace(/\/$/, '')))
    ? target.trim().replace(/\/$/, '')
    : urls[0];

  try {
    const r = await fetchWithTimeout(`${targetUrl}/accounts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform, username, password, verification_code }),
    }, 60000); // 60s — cukup buat parsing storage_state besar. Sejak v3 (8 Mei 2026)
                // semua platform cookie-based, gak ada lagi login flow yg butuh waktu lama.
    const data = await r.json().catch(() => ({}));
    return res.status(r.status).json({ ...data, _target: targetUrl });
  } catch (e) {
    return res.status(502).json({ error: 'android-scraper unreachable', detail: e.message, _target: targetUrl });
  }
});

// DELETE /api/accounts/:platform/:username — auto-route by platform
app.delete('/api/accounts/:platform/:username', optionalAuth, async (req, res) => {
  const { platform, username } = req.params;
  const { target } = req.query;

  let targetUrl, headers;
  if (platform === 'tiktok') {
    targetUrl = TIKTOK_PC_URL;
    headers = tiktokPcHeaders();
  } else {
    const urls = await getScraperUrls();
    if (!urls.length) return res.status(503).json({ error: 'no_target_configured' });
    targetUrl = (target && urls.includes(target.trim().replace(/\/$/, '')))
      ? target.trim().replace(/\/$/, '')
      : urls[0];
    headers = { 'Content-Type': 'application/json' };
  }

  try {
    const r = await fetchWithTimeout(
      `${targetUrl}/accounts/${encodeURIComponent(platform)}/${encodeURIComponent(username)}`,
      { method: 'DELETE', headers },
      15000,
    );
    const data = await r.json().catch(() => ({}));
    res.status(r.status).json(data);
  } catch (e) {
    res.status(502).json({ error: e.message });
  }
});

// POST /api/accounts/:platform/:username/reactivate
app.post('/api/accounts/:platform/:username/reactivate', optionalAuth, async (req, res) => {
  const { platform, username } = req.params;
  const { target } = req.body || {};

  let targetUrl, headers;
  if (platform === 'tiktok') {
    targetUrl = TIKTOK_PC_URL;
    headers = tiktokPcHeaders();
  } else {
    const urls = await getScraperUrls();
    if (!urls.length) return res.status(503).json({ error: 'no_target_configured' });
    targetUrl = (target && urls.includes(target.trim().replace(/\/$/, '')))
      ? target.trim().replace(/\/$/, '')
      : urls[0];
    headers = { 'Content-Type': 'application/json' };
  }

  try {
    const r = await fetchWithTimeout(
      `${targetUrl}/accounts/${encodeURIComponent(platform)}/${encodeURIComponent(username)}/reactivate`,
      { method: 'POST', headers },
      30000,
    );
    const data = await r.json().catch(() => ({}));
    res.status(r.status).json(data);
  } catch (e) {
    res.status(502).json({ error: e.message });
  }
});

// POST /api/accounts/test-connection — test 1 URL scraper (android atau tiktok-pc)
// Body: { url, type? = 'android'|'tiktok-pc' }
app.post('/api/accounts/test-connection', optionalAuth, async (req, res) => {
  const { url, type } = req.body || {};
  if (!url) return res.status(400).json({ ok: false, error: 'url required' });
  const cleanUrl = url.trim().replace(/\/$/, '');
  const headers = type === 'tiktok-pc' ? tiktokPcHeaders() : {};
  try {
    const r = await fetchWithTimeout(`${cleanUrl}/health`, { headers }, 5000);
    const data = await r.json().catch(() => ({}));
    res.json({ ok: r.ok, status: r.status, data });
  } catch (e) {
    res.status(502).json({ ok: false, error: e.message });
  }
});

// Helper: validasi platform yang didukung
function result_platforms_supported(p) {
  return ['instagram', 'tiktok', 'facebook', 'youtube', 'twitter', 'threads'].includes(p);
}

// ===== BACKWARD-COMPAT ALIASES =====
// Endpoint lama dari frontend cache lama. Cukup proxy isi req.body ke endpoint baru.
app.post('/api/android-scraper', async (req, res) => {
  // Map field lama (scraper_url tunggal) ke payload baru — abaikan scraper_url,
  // gunakan pool dari settings (perilaku baru: load-balanced).
  req.body = {
    platform: req.body.platform,
    keyword: req.body.keyword,
    max_results: req.body.max_results,
    mode: req.body.mode,
    video_url: req.body.video_url,
    sites: req.body.sites,
  };
  // Reuse the same logic by calling the handler inline
  return app._scrapeHandler(req, res);
});
app.post('/api/android-scraper/health', async (req, res) => {
  return app._scrapeHealthHandler(req, res);
});

// ===== CHECKPOINT MANAGEMENT =====
// Reset a specific platform/keyword checkpoint (for force re-scrape)
app.delete('/api/checkpoints/:project_id', async (req, res) => {
  const { platform, keyword } = req.query;
  try {
    const filter = { project_id: req.params.project_id };
    if (platform) filter.platform = platform;
    if (keyword) filter.keyword = keyword;
    
    const result = await db.collection('scrape_checkpoints').deleteMany(filter);
    res.json({ deleted: result.deletedCount });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ===== HEALTH =====
app.get('/api/health', async (req, res) => {
  try {
    const posts = await db.collection('posts').countDocuments();
    const projects = await db.collection('projects').countDocuments();
    const checkpoints = await db.collection('scrape_checkpoints').countDocuments();
    res.json({
      status: 'ok',
      database: 'mongodb',
      posts,
      projects,
      checkpoints,
      ws_subscribers: wsHub ? Object.fromEntries(
        [...wsHub.subscribers.entries()].map(([k, v]) => [k, v.size])
      ) : {},
      agent_enabled: !!process.env.OPENROUTER_API_KEY,
    });
  } catch (e) {
    res.status(500).json({ status: 'error', error: e.message });
  }
});

// =================================================================
// FASE 4 — WebSocket Broadcast Hub
// =================================================================
const http = require('http');
const { WebSocketServer } = require('ws');

const httpServer = http.createServer(app);
const wss = new WebSocketServer({ server: httpServer, path: '/ws' });

const wsHub = {
  // monitor_id -> Set<WebSocket>
  subscribers: new Map(),

  add(monitorId, ws) {
    if (!this.subscribers.has(monitorId)) this.subscribers.set(monitorId, new Set());
    this.subscribers.get(monitorId).add(ws);
    ws.on('close', () => {
      const subs = this.subscribers.get(monitorId);
      if (subs) {
        subs.delete(ws);
        if (subs.size === 0) this.subscribers.delete(monitorId);
      }
    });
  },

  broadcast(monitorId, payload) {
    const subs = this.subscribers.get(monitorId);
    if (!subs) return 0;
    let sent = 0;
    for (const ws of subs) {
      if (ws.readyState === ws.OPEN) {
        try { ws.send(JSON.stringify(payload)); sent++; } catch {}
      }
    }
    return sent;
  },

  broadcastAll(payload) {
    let sent = 0;
    for (const monitorId of this.subscribers.keys()) {
      sent += this.broadcast(monitorId, payload);
    }
    return sent;
  },
};

wss.on('connection', (ws, req) => {
  // Auth via query param ?token=jwt&monitor_id=N
  // monitor_id WAJIB; token optional kalau JWT_SECRET default (dev mode).
  let url;
  try {
    url = new URL(req.url, 'http://localhost');
  } catch {
    ws.close(1008, 'invalid_url');
    return;
  }

  const token = url.searchParams.get('token');
  const monitorId = parseInt(url.searchParams.get('monitor_id'), 10);

  if (!monitorId || monitorId < 1 || monitorId > 6) {
    ws.close(1008, 'invalid_monitor_id');
    return;
  }

  // Token verification — strict mode kalau WS_REQUIRE_AUTH=1
  if (token) {
    try {
      jwt.verify(token, JWT_SECRET);
    } catch (e) {
      if (process.env.WS_REQUIRE_AUTH === '1') {
        ws.close(1008, 'invalid_token');
        return;
      }
      // Otherwise warn but allow (dev mode)
      console.warn(`[WS] invalid token for monitor ${monitorId}: ${e.message}`);
    }
  } else if (process.env.WS_REQUIRE_AUTH === '1') {
    ws.close(1008, 'token_required');
    return;
  }

  wsHub.add(monitorId, ws);
  console.log(`[WS] Monitor ${monitorId} connected (total subs: ${wsHub.subscribers.get(monitorId).size})`);
  ws.send(JSON.stringify({ type: 'connected', monitor_id: monitorId, timestamp: Date.now() }));

  ws.on('message', (raw) => {
    try {
      const msg = JSON.parse(raw);
      if (msg.type === 'ping') ws.send(JSON.stringify({ type: 'pong', timestamp: Date.now() }));
    } catch {}
  });
});

// =================================================================
// FASE 4 — broadcast helper endpoint (manual trigger via curl/agent)
// =================================================================
app.post('/api/broadcast', authenticateToken, async (req, res) => {
  const { monitor_id, view, payload, all } = req.body || {};
  if (!view) return res.status(400).json({ error: 'view required' });

  const message = { type: 'render', view, payload: payload || {}, timestamp: Date.now() };
  let sent;
  if (all) {
    sent = wsHub.broadcastAll(message);
  } else {
    if (!monitor_id) return res.status(400).json({ error: 'monitor_id required (or set all=true)' });
    sent = wsHub.broadcast(parseInt(monitor_id, 10), message);
  }
  res.json({ broadcasted: true, subscribers_notified: sent });
});

// =================================================================
// FASE 3 — AI Agent Endpoint
// =================================================================
const { runAgent } = require('./agent');

// Rate limit untuk AI agent (Fase 6.3) — in-memory, per IP/user
const _agentRateLimits = new Map();
function checkAgentRateLimit(key, limit = 10, windowMs = 60_000) {
  const now = Date.now();
  const arr = _agentRateLimits.get(key) || [];
  const recent = arr.filter(t => now - t < windowMs);
  if (recent.length >= limit) return false;
  recent.push(now);
  _agentRateLimits.set(key, recent);
  return true;
}

app.post('/api/agent/command', optionalAuth, async (req, res) => {
  const { command, model, project_id } = req.body || {};
  if (!command || typeof command !== 'string') {
    return res.status(400).json({ error: 'command (string) required' });
  }
  if (!process.env.OPENROUTER_API_KEY) {
    return res.status(503).json({
      error: 'openrouter_not_configured',
      message: 'Set OPENROUTER_API_KEY di .env untuk mengaktifkan AI Agent.',
    });
  }

  // Rate limit per IP (atau per user kalau login)
  const rateKey = req.user?.id || req.ip || 'anonymous';
  const limit = parseInt(process.env.AGENT_RATE_LIMIT || '10', 10);
  if (!checkAgentRateLimit(rateKey, limit, 60_000)) {
    return res.status(429).json({ error: 'rate_limit_exceeded', message: `Max ${limit} command/menit. Coba lagi sebentar.` });
  }

  const startTime = Date.now();
  try {
    const ctx = {
      db,
      wsHub,
      scrapeViaScraperPool: (platform, keyword, max_results) =>
        scrapeViaScraperPool(platform, keyword, max_results),
    };
    const result = await runAgent(command, ctx, { model });

    // Persist command history (Fase 6.4)
    try {
      await db.collection('agent_history').insertOne({
        user_id: req.user?.id || null,
        project_id: project_id || null,
        command,
        final_message: result.final_message,
        iterations: result.iterations,
        usage: result.usage || null,
        duration_ms: Date.now() - startTime,
        timestamp: new Date(),
      });
    } catch (e) {
      console.warn('[agent] failed to save history:', e.message);
    }

    res.json({ ...result, duration_ms: Date.now() - startTime });
  } catch (e) {
    console.error('[agent] error:', e);
    res.status(500).json({
      error: 'agent_failed',
      detail: e.message?.slice(0, 500) || 'unknown',
      duration_ms: Date.now() - startTime,
    });
  }
});

// GET command history (untuk Monitor 6 — replay)
app.get('/api/agent/history', optionalAuth, async (req, res) => {
  try {
    const limit = parseInt(req.query.limit || '50', 10);
    const filter = {};
    if (req.user?.id) filter.user_id = req.user.id;
    const history = await db.collection('agent_history')
      .find(filter)
      .sort({ timestamp: -1 })
      .limit(limit)
      .toArray();
    res.json({ history });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Start server (HTTP + WS)

// ============================================================================
// MONITOR DATA ENDPOINT
// One-shot endpoint untuk auto-load semua data monitor tanpa nunggu JARVIS push.
// Dipakai oleh setiap monitor (M1-M5) saat boot — fetch sekali, render, lalu
// listen WS update untuk perubahan berikutnya.
// ============================================================================

app.get('/api/monitor/data', optionalAuth, async (req, res) => {
  try {
    const { project_id, keyword } = req.query;
    if (!project_id && !keyword) {
      return res.status(400).json({ error: 'project_id atau keyword harus ada' });
    }

    // Build filter — kalau ada project_id, pakai itu; kalau ada keyword, match keyword_matched
    const filter = {};
    if (project_id) filter.project_id = project_id;
    if (keyword) filter.keyword_matched = { $regex: keyword, $options: 'i' };

    // Run 6 aggregation paralel — 1 round-trip dari client
    const [
      totalCount,
      sentimentAgg,
      platformAgg,
      topInfluencers,
      viralPosts,
      mentionsFeed,
      newsFeed,
    ] = await Promise.all([
      // Total mentions
      db.collection('posts').countDocuments(filter),

      // Sentiment breakdown
      db.collection('posts').aggregate([
        { $match: filter },
        { $group: { _id: '$sentiment', count: { $sum: 1 } } },
      ]).toArray(),

      // Platform breakdown
      db.collection('posts').aggregate([
        { $match: filter },
        { $group: { _id: '$platform', count: { $sum: 1 } } },
      ]).toArray(),

      // Top 5 influencers
      db.collection('posts').aggregate([
        { $match: filter },
        { $group: {
          _id: { author: '$author', handle: '$handle' },
          platforms: { $addToSet: '$platform' },
          post_count: { $sum: 1 },
          total_likes: { $sum: { $ifNull: ['$likes', 0] } },
          total_views: { $sum: { $ifNull: ['$views', 0] } },
          total_comments: { $sum: { $ifNull: ['$comments', 0] } },
          avatar: { $first: '$avatar' },
        }},
        { $addFields: { total_reach: { $add: ['$total_likes', '$total_views', '$total_comments'] } } },
        { $sort: { total_reach: -1 } },
        { $limit: 10 },
        { $project: {
          _id: 0,
          author: '$_id.author', handle: '$_id.handle', avatar: 1,
          platforms: 1, post_count: 1,
          total_likes: 1, total_views: 1, total_comments: 1, total_reach: 1,
        }},
      ]).toArray(),

      // Top 5 viral posts
      db.collection('posts').aggregate([
        { $match: filter },
        { $addFields: { engagement: { $add: [
          { $ifNull: ['$likes', 0] },
          { $ifNull: ['$comments', 0] },
          { $ifNull: ['$views', 0] },
          { $ifNull: ['$shares', 0] },
        ]}}},
        { $sort: { engagement: -1 } },
        { $limit: 5 },
        { $project: { _id: 0, platform: 1, author: 1, handle: 1, avatar: 1,
          content: 1, url: 1, likes: 1, views: 1, comments: 1, shares: 1,
          engagement: 1, sentiment: 1, post_date: 1 } },
      ]).toArray(),

      // Mentions feed (recent 30)
      db.collection('posts')
        .find(filter)
        .sort({ post_date: -1, created_at: -1 })
        .limit(30)
        .project({ _id: 0, platform: 1, author: 1, handle: 1, avatar: 1,
          content: 1, url: 1, sentiment: 1, post_date: 1, likes: 1, comments: 1, views: 1 })
        .toArray(),

      // News feed (top 10)
      db.collection('posts')
        .find({ ...filter, platform: 'news' })
        .sort({ post_date: -1, created_at: -1 })
        .limit(10)
        .project({ _id: 0, author: 1, content: 1, url: 1, source_name: 1, post_date: 1, sentiment: 1 })
        .toArray(),
    ]);

    // Sentiment summary normalized
    const sCounts = { positive: 0, neutral: 0, negative: 0 };
    for (const row of sentimentAgg) {
      if (sCounts.hasOwnProperty(row._id)) sCounts[row._id] = row.count;
      else sCounts.neutral += row.count;
    }
    const sTotal = sCounts.positive + sCounts.neutral + sCounts.negative;
    const sentiment = {
      total: sTotal,
      counts: sCounts,
      percentages: {
        positive: sTotal ? +(sCounts.positive / sTotal * 100).toFixed(1) : 0,
        neutral: sTotal ? +(sCounts.neutral / sTotal * 100).toFixed(1) : 0,
        negative: sTotal ? +(sCounts.negative / sTotal * 100).toFixed(1) : 0,
      },
    };

    // Platforms map
    const platforms = {};
    for (const row of platformAgg) if (row._id) platforms[row._id] = row.count;

    // Build SNA (sederhana — pakai posts terbatas)
    const snaPosts = await db.collection('posts')
      .find(filter)
      .limit(300)
      .project({ author: 1, handle: 1, platform: 1, hashtags: 1, likes: 1, views: 1, comments: 1 })
      .toArray();
    const authorMap = new Map();
    for (const p of snaPosts) {
      if (!p.author) continue;
      const e = authorMap.get(p.author) || { author: p.author, handle: p.handle, platforms: new Set(),
        post_count: 0, total_reach: 0, hashtags: new Set() };
      e.platforms.add(p.platform);
      e.post_count++;
      e.total_reach += (p.likes || 0) + (p.views || 0) + (p.comments || 0);
      let tags = [];
      try { tags = typeof p.hashtags === 'string' ? JSON.parse(p.hashtags) : (p.hashtags || []); } catch {}
      if (Array.isArray(tags)) tags.forEach(t => e.hashtags.add(String(t).toLowerCase()));
      authorMap.set(p.author, e);
    }
    const snaNodes = Array.from(authorMap.values())
      .sort((a, b) => b.total_reach - a.total_reach)
      .slice(0, 30)
      .map(n => ({ id: n.author, label: n.author, handle: n.handle,
        platforms: Array.from(n.platforms), post_count: n.post_count, total_reach: n.total_reach }));
    const snaEdges = [];
    const aArr = snaNodes.map(n => ({ id: n.id, hashtags: authorMap.get(n.id).hashtags }));
    for (let i = 0; i < aArr.length; i++) {
      for (let j = i + 1; j < aArr.length; j++) {
        let shared = 0;
        for (const h of aArr[i].hashtags) if (aArr[j].hashtags.has(h)) shared++;
        if (shared > 0) snaEdges.push({ source: aArr[i].id, target: aArr[j].id, weight: shared });
      }
    }

    res.json({
      filter: { project_id: project_id || null, keyword: keyword || null },
      total_mentions: totalCount,
      sentiment,
      platforms,
      top_3_influencers: topInfluencers.slice(0, 3),
      top_influencers: topInfluencers,
      viral_top_1: viralPosts[0] || null,
      viral_posts: viralPosts,
      mentions: mentionsFeed,
      news: newsFeed,
      sna: { nodes: snaNodes, edges: snaEdges, total_posts_analyzed: snaPosts.length },
    });
  } catch (e) {
    console.error('[monitor/data] failed:', e);
    res.status(500).json({ error: e.message });
  }
});

// ============================================================================
// 24/7 SCHEDULER + NOTIFIER ENDPOINTS
// ============================================================================

// GET status scheduler
app.get('/api/scheduler/status', optionalAuth, (req, res) => {
  res.json(scheduler.status());
});

// Trigger cycle manual (untuk testing tanpa nunggu interval)
app.post('/api/scheduler/run-now', authenticateToken, async (req, res) => {
  const ctx = {
    db,
    wsHub,
    scrapeViaScraperPool: (platform, keyword, max_results) =>
      scrapeViaScraperPool(platform, keyword, max_results),
  };
  const result = await scheduler.triggerNow(ctx);
  res.json(result);
});

// Trigger daily briefing manual (untuk test Telegram setup)
app.post('/api/scheduler/briefing-now', authenticateToken, async (req, res) => {
  const ctx = { db, wsHub };
  await scheduler.runDailyBriefing(ctx);
  res.json({ sent: true });
});

// Test notification — kirim alert dummy ke Telegram/Discord untuk verifikasi config
app.post('/api/notifier/test', authenticateToken, async (req, res) => {
  const result = await dispatchAlert({
    type: 'test_alert',
    severity: 'high',
    keyword: 'test',
    message: 'Ini test alert dari Slaytics. Kalau Bos terima, berarti notifikasi sudah jalan.',
    metrics: { test: true },
    detected_at: new Date(),
  }, { wsHub, db });
  res.json(result);
});

// GET history alerts (24 jam terakhir, untuk display di UI)
app.get('/api/alerts/recent', optionalAuth, async (req, res) => {
  try {
    const hours = parseInt(req.query.hours || '24', 10);
    const since = new Date(Date.now() - hours * 3600 * 1000);
    const alerts = await db.collection('alerts')
      .find({ detected_at: { $gte: since } })
      .sort({ detected_at: -1 })
      .limit(100)
      .toArray();
    res.json({ alerts });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

connectDB().then(async () => {
  // Initialize unified config helper — read from DB, fallback to env
  config.init(db);
  await config.migrateFromEnv();   // populate DB with env values on first boot

  // ─── Data recovery untuk install yang sebelumnya kena bug routing ───
  // Pre-fix: PUT /api/settings/all match handler lama `/:key` → save sebagai
  // doc { key: 'all', value: '<JSON blob>' } di collection 'settings'.
  // Sekarang kita salvage data itu dan migrate ke collection 'app_settings'.
  try {
    const stale = await db.collection('settings').findOne({ key: 'all' });
    if (stale && stale.value && typeof stale.value === 'object') {
      console.log('[config] found stale settings from old routing bug, recovering…');
      await config.save(stale.value);
      await db.collection('settings').deleteOne({ key: 'all' });
      console.log('[config] recovered & cleaned up — settings now in correct collection');
    }
  } catch (e) {
    console.warn('[config] recovery failed (non-fatal):', e.message);
  }

  await config.loadAll();           // warm cache

  httpServer.listen(PORT, '0.0.0.0', () => {
    console.log(`Slaytics API v2.0 (MongoDB) on :${PORT} (HTTP + WS at /ws)`);

    if (!config.get('openrouter_api_key')) {
      console.warn('[!] OpenRouter API key not set — set it in Settings.');
    }
    if (!config.get('telegram_bot_token')) {
      console.warn('[!] Telegram not configured — set it in Settings.');
    } else {
      console.log('[notifier] Telegram alerts enabled');
    }
    if (!config.get('discord_webhook_url')) {
      console.warn('[!] Discord webhook not configured — set it in Settings (optional).');
    } else {
      console.log('[notifier] Discord alerts enabled');
    }

    // Start 24/7 scheduler (auto-restart on config change)
    const ctx = {
      db,
      wsHub,
      scrapeViaScraperPool: (platform, keyword, max_results) =>
        scrapeViaScraperPool(platform, keyword, max_results),
    };
    scheduler.start(ctx);
  });
});