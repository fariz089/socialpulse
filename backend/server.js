const express = require('express');
const cors = require('cors');
const { MongoClient, ObjectId } = require('mongodb');
const bcrypt = require('bcryptjs');
const jwt = require('jsonwebtoken');

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
    
    const token = jwt.sign({ id: result.insertedId.toString(), username, email }, JWT_SECRET, { expiresIn: '7d' });
    
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
    }, JWT_SECRET, { expiresIn: '7d' });
    
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

app.put('/api/projects/:id', async (req, res) => {
  const { name, keywords, platforms, language, color, excluded_keywords } = req.body;
  try {
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

app.delete('/api/projects/:id', async (req, res) => {
  try {
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
      res.json({ last_date: null, today, gap_days: null, needs_scrape: true, is_first_scrape: true });
    } else {
      const lastDate = new Date(checkpoint.last_scraped_date);
      const todayDate = new Date(today);
      const gapDays = Math.floor((todayDate - lastDate) / (1000 * 60 * 60 * 24));
      res.json({
        last_date: checkpoint.last_scraped_date,
        today,
        gap_days: gapDays,
        needs_scrape: gapDays > 0,
        is_first_scrape: false,
        suggested_from: gapDays > 0 ? new Date(lastDate.getTime() + 86400000).toISOString().split('T')[0] : null
      });
    }
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ===== APIFY PROXY =====
app.post('/api/apify-proxy', async (req, res) => {
  const { token, actor, input } = req.body;
  
  if (!token || !actor) {
    return res.status(400).json({ error: 'Token and actor required' });
  }
  
  try {
    const fetch = (await import('node-fetch')).default;
    const actorId = actor.replace('/', '~');
    const url = `https://api.apify.com/v2/acts/${actorId}/run-sync-get-dataset-items?token=${token}`;
    
    console.log(`[Apify Proxy] Calling actor: ${actorId}`);
    
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

// ===== HEALTH =====
app.get('/api/health', async (req, res) => {
  try {
    const posts = await db.collection('posts').countDocuments();
    const projects = await db.collection('projects').countDocuments();
    const checkpoints = await db.collection('scrape_checkpoints').countDocuments();
    res.json({ status: 'ok', database: 'mongodb', posts, projects, checkpoints });
  } catch (e) {
    res.status(500).json({ status: 'error', error: e.message });
  }
});

// Start server
connectDB().then(() => {
  app.listen(PORT, '0.0.0.0', () => console.log(`Slaytics API v2.0 (MongoDB) on :${PORT}`));
});