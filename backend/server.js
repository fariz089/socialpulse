const express = require('express');
const cors = require('cors');
const Database = require('better-sqlite3');
const path = require('path');
const bcrypt = require('bcryptjs');
const jwt = require('jsonwebtoken');

const app = express();
const PORT = process.env.PORT || 3001;
const DB_PATH = process.env.DB_PATH || path.join(__dirname, 'data', 'slaytics.db');
const JWT_SECRET = process.env.JWT_SECRET || 'slaytics-secret-key-change-in-production-2024';

app.use(cors());
app.use(express.json({ limit: '50mb' }));

const db = new Database(DB_PATH);
db.pragma('journal_mode = WAL');
db.pragma('foreign_keys = ON');

db.exec(`
  CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    role TEXT DEFAULT 'user',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
  );
  CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
  );
  CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT NOT NULL,
    keywords TEXT NOT NULL,
    language TEXT DEFAULT 'id',
    excluded_keywords TEXT DEFAULT '[]',
    platforms TEXT DEFAULT '["tiktok","twitter","instagram","news"]',
    color TEXT DEFAULT '#6366f1',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
  );
  CREATE TABLE IF NOT EXISTS scrape_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    platforms TEXT NOT NULL,
    date_from TEXT, date_to TEXT,
    max_results INTEGER DEFAULT 10,
    total_results INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
  );
  CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    project_id INTEGER NOT NULL,
    external_id TEXT,
    platform TEXT NOT NULL,
    keyword_matched TEXT,
    author TEXT, handle TEXT, avatar TEXT, content TEXT,
    views INTEGER DEFAULT 0, likes INTEGER DEFAULT 0,
    shares INTEGER DEFAULT 0, comments INTEGER DEFAULT 0,
    sentiment TEXT DEFAULT 'neutral',
    influence_score REAL DEFAULT 0,
    cities TEXT DEFAULT '[]',
    hashtags TEXT DEFAULT '[]',
    source_name TEXT, url TEXT,
    post_date DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES scrape_sessions(id) ON DELETE SET NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
  );
  CREATE TABLE IF NOT EXISTS scrape_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    keyword TEXT NOT NULL,
    last_scraped_date TEXT NOT NULL,
    last_scraped_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    total_posts_scraped INTEGER DEFAULT 0,
    UNIQUE(project_id, platform, keyword),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
  );
  CREATE INDEX IF NOT EXISTS idx_posts_project ON posts(project_id);
  CREATE INDEX IF NOT EXISTS idx_posts_platform ON posts(platform);
  CREATE INDEX IF NOT EXISTS idx_posts_sentiment ON posts(sentiment);
  CREATE INDEX IF NOT EXISTS idx_posts_date ON posts(post_date);
  CREATE INDEX IF NOT EXISTS idx_posts_session ON posts(session_id);
  CREATE INDEX IF NOT EXISTS idx_posts_external ON posts(external_id);
  CREATE INDEX IF NOT EXISTS idx_checkpoints_project ON scrape_checkpoints(project_id);
  CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id);
`);

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

// Optional auth - doesn't require token but attaches user if present
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
    const existing = db.prepare('SELECT id FROM users WHERE username = ? OR email = ?').get(username, email);
    if (existing) {
      return res.status(400).json({ error: 'Username or email already exists' });
    }
    
    const hashedPassword = await bcrypt.hash(password, 10);
    const result = db.prepare('INSERT INTO users (username, email, password) VALUES (?, ?, ?)').run(username, email, hashedPassword);
    
    const token = jwt.sign({ id: result.lastInsertRowid, username, email }, JWT_SECRET, { expiresIn: '7d' });
    
    res.json({ 
      success: true, 
      token,
      user: { id: result.lastInsertRowid, username, email }
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
    const user = db.prepare('SELECT * FROM users WHERE username = ? OR email = ?').get(username, username);
    
    if (!user) {
      return res.status(401).json({ error: 'Invalid credentials' });
    }
    
    const validPassword = await bcrypt.compare(password, user.password);
    if (!validPassword) {
      return res.status(401).json({ error: 'Invalid credentials' });
    }
    
    const token = jwt.sign({ id: user.id, username: user.username, email: user.email }, JWT_SECRET, { expiresIn: '7d' });
    
    res.json({ 
      success: true, 
      token,
      user: { id: user.id, username: user.username, email: user.email }
    });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get('/api/auth/me', authenticateToken, (req, res) => {
  const user = db.prepare('SELECT id, username, email, role, created_at FROM users WHERE id = ?').get(req.user.id);
  if (!user) return res.status(404).json({ error: 'User not found' });
  res.json(user);
});

app.put('/api/auth/password', authenticateToken, async (req, res) => {
  const { currentPassword, newPassword } = req.body;
  
  if (!currentPassword || !newPassword) {
    return res.status(400).json({ error: 'Current and new password required' });
  }
  
  try {
    const user = db.prepare('SELECT password FROM users WHERE id = ?').get(req.user.id);
    const valid = await bcrypt.compare(currentPassword, user.password);
    if (!valid) {
      return res.status(401).json({ error: 'Current password is incorrect' });
    }
    
    const hashed = await bcrypt.hash(newPassword, 10);
    db.prepare('UPDATE users SET password = ? WHERE id = ?').run(hashed, req.user.id);
    res.json({ success: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ===== SETTINGS =====
app.get('/api/settings', optionalAuth, (req, res) => {
  const rows = db.prepare('SELECT key, value FROM settings').all();
  const s = {}; rows.forEach(r => { try { s[r.key] = JSON.parse(r.value); } catch { s[r.key] = r.value; } });
  res.json(s);
});
app.get('/api/settings/:key', optionalAuth, (req, res) => {
  const row = db.prepare('SELECT value FROM settings WHERE key = ?').get(req.params.key);
  if (!row) return res.json({ value: null });
  try { res.json({ value: JSON.parse(row.value) }); } catch { res.json({ value: row.value }); }
});
app.put('/api/settings/:key', optionalAuth, (req, res) => {
  const val = typeof req.body.value === 'string' ? req.body.value : JSON.stringify(req.body.value);
  db.prepare(`INSERT INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP`).run(req.params.key, val);
  res.json({ success: true });
});

// ===== PROJECTS =====
const parseProject = p => {
  try { p.keywords = JSON.parse(p.keywords); } catch { p.keywords = [p.keywords]; }
  try { p.excluded_keywords = JSON.parse(p.excluded_keywords); } catch { p.excluded_keywords = []; }
  try { p.platforms = JSON.parse(p.platforms); } catch { p.platforms = []; }
  return p;
};

app.get('/api/projects', (req, res) => {
  const projects = db.prepare(`SELECT p.*,
    (SELECT COUNT(*) FROM posts WHERE project_id=p.id) as total_mentions,
    (SELECT COALESCE(SUM(views),0) FROM posts WHERE project_id=p.id) as total_reach,
    (SELECT MAX(created_at) FROM scrape_sessions WHERE project_id=p.id) as last_scrape
    FROM projects p ORDER BY p.updated_at DESC`).all();
  res.json(projects.map(parseProject));
});

app.get('/api/projects/:id', (req, res) => {
  const p = db.prepare('SELECT * FROM projects WHERE id = ?').get(req.params.id);
  if (!p) return res.status(404).json({ error: 'Not found' });
  res.json(parseProject(p));
});

app.post('/api/projects', (req, res) => {
  const { name, keywords, language, excluded_keywords, platforms, color } = req.body;
  const kw = Array.isArray(keywords) ? keywords : keywords.split(',').map(k => k.trim()).filter(Boolean);
  const r = db.prepare(`INSERT INTO projects (name, keywords, language, excluded_keywords, platforms, color) VALUES (?,?,?,?,?,?)`)
    .run(name, JSON.stringify(kw), language||'id', JSON.stringify(excluded_keywords||[]), JSON.stringify(platforms||['tiktok','twitter','instagram','news']), color||'#6366f1');
  res.json({ id: r.lastInsertRowid, name, keywords: kw });
});

app.put('/api/projects/:id', (req, res) => {
  const { name, keywords, language, excluded_keywords, platforms, color } = req.body;
  const sets = []; const params = [];
  if (name !== undefined) { sets.push('name=?'); params.push(name); }
  if (keywords !== undefined) { const kw = Array.isArray(keywords)?keywords:keywords.split(',').map(k=>k.trim()).filter(Boolean); sets.push('keywords=?'); params.push(JSON.stringify(kw)); }
  if (language !== undefined) { sets.push('language=?'); params.push(language); }
  if (excluded_keywords !== undefined) { sets.push('excluded_keywords=?'); params.push(JSON.stringify(excluded_keywords)); }
  if (platforms !== undefined) { sets.push('platforms=?'); params.push(JSON.stringify(platforms)); }
  if (color !== undefined) { sets.push('color=?'); params.push(color); }
  sets.push('updated_at=CURRENT_TIMESTAMP'); params.push(req.params.id);
  db.prepare(`UPDATE projects SET ${sets.join(',')} WHERE id=?`).run(...params);
  res.json({ success: true });
});

app.delete('/api/projects/:id', (req, res) => {
  db.prepare('DELETE FROM projects WHERE id=?').run(req.params.id);
  res.json({ success: true });
});

// ===== SESSIONS =====
app.post('/api/sessions', (req, res) => {
  const { project_id, platforms, date_from, date_to, max_results } = req.body;
  const r = db.prepare(`INSERT INTO scrape_sessions (project_id,platforms,date_from,date_to,max_results) VALUES (?,?,?,?,?)`)
    .run(project_id, JSON.stringify(platforms), date_from||null, date_to||null, max_results||10);
  res.json({ id: r.lastInsertRowid });
});
app.put('/api/sessions/:id', (req, res) => {
  db.prepare('UPDATE scrape_sessions SET status=?, total_results=? WHERE id=?').run(req.body.status, req.body.total_results, req.params.id);
  res.json({ success: true });
});
app.get('/api/sessions', (req, res) => {
  const { project_id } = req.query;
  let q = 'SELECT * FROM scrape_sessions'; const p = [];
  if (project_id) { q += ' WHERE project_id=?'; p.push(project_id); }
  q += ' ORDER BY created_at DESC LIMIT 50';
  const sessions = db.prepare(q).all(...p);
  sessions.forEach(s => { try { s.platforms = JSON.parse(s.platforms); } catch {} });
  res.json(sessions);
});

// ===== POSTS =====
app.post('/api/posts/bulk', (req, res) => {
  const { posts: data, session_id, project_id } = req.body;
  const ins = db.prepare(`INSERT OR IGNORE INTO posts (session_id,project_id,external_id,platform,keyword_matched,author,handle,avatar,content,views,likes,shares,comments,sentiment,influence_score,cities,hashtags,source_name,url,post_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`);
  const tx = db.transaction(items => {
    let c = 0;
    for (const p of items) {
      const r = ins.run(session_id||null,project_id,p.external_id||p.id||null,p.platform,p.keyword_matched||null,p.author,p.handle||'',p.avatar||null,p.content||'',p.views||0,p.likes||0,p.shares||0,p.comments||0,p.sentiment||'neutral',p.influence_score||0,JSON.stringify(p.cities||[]),JSON.stringify(p.hashtags||[]),p.source_name||p.source||'',p.url||'',p.post_date||null);
      if (r.changes>0) c++;
    }
    return c;
  });
  const inserted = tx(data);
  if (session_id) {
    const t = db.prepare('SELECT COUNT(*) as c FROM posts WHERE session_id=?').get(session_id);
    db.prepare('UPDATE scrape_sessions SET total_results=? WHERE id=?').run(t.c, session_id);
  }
  res.json({ inserted, total: data.length });
});

app.get('/api/posts', (req, res) => {
  const { project_id, session_id, platform, sentiment, date_from, date_to, keyword, page=1, limit=50, sort='views' } = req.query;
  let w = ['1=1']; let p = [];
  if (project_id) { w.push('project_id=?'); p.push(project_id); }
  if (session_id) { w.push('session_id=?'); p.push(session_id); }
  if (platform) { w.push('platform=?'); p.push(platform); }
  if (sentiment) { w.push('sentiment=?'); p.push(sentiment); }
  if (date_from) { w.push('post_date>=?'); p.push(date_from); }
  if (date_to) { w.push('post_date<=?'); p.push(date_to+'T23:59:59'); }
  if (keyword) { w.push('content LIKE ?'); p.push(`%${keyword}%`); }
  const wc = w.join(' AND ');
  const cnt = db.prepare(`SELECT COUNT(*) as total FROM posts WHERE ${wc}`).get(...p);
  const sorts = {views:'views DESC',likes:'likes DESC',date:'post_date DESC',shares:'shares DESC',recent:'created_at DESC'};
  const off = (parseInt(page)-1)*parseInt(limit);
  const posts = db.prepare(`SELECT * FROM posts WHERE ${wc} ORDER BY ${sorts[sort]||'views DESC'} LIMIT ? OFFSET ?`).all(...p,parseInt(limit),off);
  posts.forEach(post => { try{post.cities=JSON.parse(post.cities||'[]');}catch{post.cities=[];} try{post.hashtags=JSON.parse(post.hashtags||'[]');}catch{post.hashtags=[];} });
  res.json({ posts, total: cnt.total, page: parseInt(page), limit: parseInt(limit) });
});

// ===== STATS =====
app.get('/api/stats/:project_id', (req, res) => {
  const { date_from, date_to, platform } = req.query;
  let w = ['project_id=?']; let p = [req.params.project_id];
  if (date_from) { w.push('post_date>=?'); p.push(date_from); }
  if (date_to) { w.push('post_date<=?'); p.push(date_to+'T23:59:59'); }
  if (platform) { w.push('platform=?'); p.push(platform); }
  const wc = w.join(' AND ');

  const totals = db.prepare(`SELECT COUNT(*) as total, COALESCE(SUM(views),0) as total_views, COALESCE(SUM(likes),0) as total_likes, COALESCE(SUM(shares),0) as total_shares, COALESCE(SUM(comments),0) as total_comments, SUM(CASE WHEN sentiment='positive' THEN 1 ELSE 0 END) as positive, SUM(CASE WHEN sentiment='negative' THEN 1 ELSE 0 END) as negative, SUM(CASE WHEN sentiment='neutral' THEN 1 ELSE 0 END) as neutral FROM posts WHERE ${wc}`).get(...p);
  const byPlatform = db.prepare(`SELECT platform, COUNT(*) as count, COALESCE(SUM(views),0) as views FROM posts WHERE ${wc} GROUP BY platform ORDER BY count DESC`).all(...p);
  const byDate = db.prepare(`SELECT DATE(post_date) as date, platform, COUNT(*) as count, COALESCE(SUM(views),0) as views FROM posts WHERE ${wc} AND post_date IS NOT NULL GROUP BY DATE(post_date), platform ORDER BY date`).all(...p);
  const sentimentByDate = db.prepare(`SELECT DATE(post_date) as date, sentiment, COUNT(*) as count FROM posts WHERE ${wc} AND post_date IS NOT NULL GROUP BY DATE(post_date), sentiment ORDER BY date`).all(...p);
  const topAuthors = db.prepare(`SELECT author, platform, COUNT(*) as post_count, COALESCE(SUM(views),0) as views, SUM(CASE WHEN sentiment='positive' THEN 1 ELSE 0 END) as positive, SUM(CASE WHEN sentiment='negative' THEN 1 ELSE 0 END) as negative FROM posts WHERE ${wc} GROUP BY author ORDER BY views DESC LIMIT 20`).all(...p);
  const hourly = db.prepare(`SELECT CAST(strftime('%H',post_date) AS INTEGER) as hour, COUNT(*) as count FROM posts WHERE ${wc} AND post_date IS NOT NULL GROUP BY hour ORDER BY hour`).all(...p);
  const sources = db.prepare(`SELECT source_name, COUNT(*) as count FROM posts WHERE ${wc} AND source_name!='' GROUP BY source_name ORDER BY count DESC LIMIT 15`).all(...p);

  const hashRows = db.prepare(`SELECT hashtags FROM posts WHERE ${wc} AND hashtags!='[]'`).all(...p);
  const hashMap = {};
  hashRows.forEach(r => { try { JSON.parse(r.hashtags).forEach(h => hashMap[h.toLowerCase()]=(hashMap[h.toLowerCase()]||0)+1); } catch {} });

  res.json({ totals, byPlatform, byDate, sentimentByDate, topAuthors, hourly, topHashtags: hashMap, sources });
});

// ===== COMPARISON =====
app.post('/api/compare', (req, res) => {
  const { project_ids, date_from, date_to } = req.body;
  if (!project_ids || project_ids.length < 2) return res.status(400).json({ error: 'Need >=2 projects' });

  const results = project_ids.map(pid => {
    let w = ['project_id=?']; let p = [pid];
    if (date_from) { w.push('post_date>=?'); p.push(date_from); }
    if (date_to) { w.push('post_date<=?'); p.push(date_to+'T23:59:59'); }
    const wc = w.join(' AND ');
    const project = db.prepare('SELECT id,name,keywords,color FROM projects WHERE id=?').get(pid);
    if (!project) return null;
    try { project.keywords = JSON.parse(project.keywords); } catch {}

    const totals = db.prepare(`SELECT COUNT(*) as mentions, COALESCE(SUM(views),0) as reach, COALESCE(SUM(likes),0) as likes, COALESCE(SUM(shares),0) as shares, SUM(CASE WHEN sentiment='positive' THEN 1 ELSE 0 END) as positive, SUM(CASE WHEN sentiment='negative' THEN 1 ELSE 0 END) as negative, SUM(CASE WHEN sentiment='neutral' THEN 1 ELSE 0 END) as neutral FROM posts WHERE ${wc}`).get(...p);
    const byPlatform = db.prepare(`SELECT platform, COUNT(*) as count FROM posts WHERE ${wc} GROUP BY platform`).all(...p);
    const byDate = db.prepare(`SELECT DATE(post_date) as date, COUNT(*) as count FROM posts WHERE ${wc} AND post_date IS NOT NULL GROUP BY DATE(post_date) ORDER BY date`).all(...p);
    const sentimentByDate = db.prepare(`SELECT DATE(post_date) as date, sentiment, COUNT(*) as count FROM posts WHERE ${wc} AND post_date IS NOT NULL GROUP BY DATE(post_date), sentiment ORDER BY date`).all(...p);

    const presenceScore = Math.min(100, Math.round((totals.mentions*0.3 + totals.reach*0.00001 + totals.likes*0.001 + (totals.positive/(totals.mentions||1))*30)));
    return { project, totals: { ...totals, presenceScore }, byPlatform, byDate, sentimentByDate };
  }).filter(Boolean);

  const totalMentions = results.reduce((s,r) => s+r.totals.mentions, 0) || 1;
  results.forEach(r => { r.totals.shareOfVoice = Math.round((r.totals.mentions/totalMentions)*100); });
  res.json({ projects: results, period: { date_from, date_to } });
});

// ===== CHECKPOINTS (Incremental Scraping) =====
app.get('/api/checkpoints/:project_id', (req, res) => {
  const rows = db.prepare('SELECT * FROM scrape_checkpoints WHERE project_id = ? ORDER BY platform, keyword').all(req.params.project_id);
  res.json(rows);
});

app.get('/api/checkpoints/:project_id/summary', (req, res) => {
  // Get overall last scrape info per platform
  const summary = db.prepare(`
    SELECT platform, 
           MAX(last_scraped_date) as last_date,
           MAX(last_scraped_at) as last_at,
           SUM(total_posts_scraped) as total_posts
    FROM scrape_checkpoints 
    WHERE project_id = ? 
    GROUP BY platform
  `).all(req.params.project_id);
  
  // Get the absolute latest scrape across all platforms
  const latest = db.prepare(`
    SELECT MAX(last_scraped_date) as last_date, MAX(last_scraped_at) as last_at
    FROM scrape_checkpoints WHERE project_id = ?
  `).get(req.params.project_id);
  
  res.json({ byPlatform: summary, latest });
});

app.post('/api/checkpoints', (req, res) => {
  const { project_id, platform, keyword, last_scraped_date, posts_count } = req.body;
  db.prepare(`
    INSERT INTO scrape_checkpoints (project_id, platform, keyword, last_scraped_date, total_posts_scraped, last_scraped_at) 
    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(project_id, platform, keyword) DO UPDATE SET 
      last_scraped_date = excluded.last_scraped_date,
      last_scraped_at = CURRENT_TIMESTAMP,
      total_posts_scraped = total_posts_scraped + excluded.total_posts_scraped
  `).run(project_id, platform, keyword, last_scraped_date, posts_count || 0);
  res.json({ success: true });
});

app.get('/api/checkpoints/:project_id/gap', (req, res) => {
  const { platform, keyword } = req.query;
  let q = 'SELECT last_scraped_date FROM scrape_checkpoints WHERE project_id = ?';
  const params = [req.params.project_id];
  
  if (platform) { q += ' AND platform = ?'; params.push(platform); }
  if (keyword) { q += ' AND keyword = ?'; params.push(keyword); }
  q += ' ORDER BY last_scraped_date DESC LIMIT 1';
  
  const row = db.prepare(q).get(...params);
  const today = new Date().toISOString().split('T')[0];
  
  if (!row) {
    // Never scraped - return null to signal full scrape needed
    res.json({ last_date: null, today, gap_days: null, needs_scrape: true, is_first_scrape: true });
  } else {
    const lastDate = new Date(row.last_scraped_date);
    const todayDate = new Date(today);
    const gapDays = Math.floor((todayDate - lastDate) / (1000 * 60 * 60 * 24));
    res.json({ 
      last_date: row.last_scraped_date, 
      today, 
      gap_days: gapDays,
      needs_scrape: gapDays > 0,
      is_first_scrape: false,
      suggested_from: gapDays > 0 ? new Date(lastDate.getTime() + 86400000).toISOString().split('T')[0] : null
    });
  }
});

// ===== APIFY PROXY (for CORS-blocked actors like Facebook) =====
app.post('/api/apify-proxy', async (req, res) => {
  const { token, actor, input } = req.body;
  
  if (!token || !actor) {
    return res.status(400).json({ error: 'Token and actor required' });
  }
  
  try {
    const fetch = (await import('node-fetch')).default;
    
    // Convert actor name format: "username/actor-name" or "username~actor-name"
    // API needs format: "username~actor-name" for the endpoint
    const actorId = actor.replace('/', '~');
    
    // Use the synchronous run endpoint that waits for results
    const url = `https://api.apify.com/v2/acts/${actorId}/run-sync-get-dataset-items?token=${token}`;
    
    console.log(`[Apify Proxy] Calling actor: ${actorId}`);
    console.log(`[Apify Proxy] URL: ${url}`);
    console.log(`[Apify Proxy] Input:`, JSON.stringify(input));
    
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input)
    });
    
    if (!response.ok) {
      const errorText = await response.text();
      console.error(`[Apify Proxy] Error (${response.status}): ${errorText}`);
      return res.status(response.status).json({ error: errorText });
    }
    
    const data = await response.json();
    console.log(`[Apify Proxy] Success: ${Array.isArray(data) ? data.length : 'N/A'} items`);
    res.json(data);
  } catch (e) {
    console.error('[Apify Proxy] Exception:', e.message);
    res.status(500).json({ error: e.message });
  }
});

app.get('/api/health', (req, res) => {
  const c = db.prepare('SELECT COUNT(*) as posts FROM posts').get();
  const p = db.prepare('SELECT COUNT(*) as projects FROM projects').get();
  const cp = db.prepare('SELECT COUNT(*) as checkpoints FROM scrape_checkpoints').get();
  res.json({ status:'ok', posts:c.posts, projects:p.projects, checkpoints: cp.checkpoints });
});

app.listen(PORT, '0.0.0.0', () => console.log(`Slaytics API v1.0 on :${PORT}`));
