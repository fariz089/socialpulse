// backend/agent.js
// AI Agent dengan tool calling via OpenRouter — JARVIS edition.
//
// Konfigurasi (model, API key, max iterations) dibaca dari config helper:
// DB → env fallback. Bisa diubah lewat UI Settings tanpa restart container.

const config = require('./config');

// ========================================================================
// TOOL DEFINITIONS (OpenAI-compatible function calling schema)
// ========================================================================
const TOOLS = [
  {
    type: 'function',
    function: {
      name: 'search_existing_posts',
      description: 'Cek post yang sudah ada di database untuk keyword tertentu. Return summary count per platform & timestamp data terakhir. Gunakan ini DULU sebelum scrape supaya tidak buang quota.',
      parameters: {
        type: 'object',
        properties: {
          keyword: { type: 'string', description: 'Keyword/hashtag yang dicari (matched terhadap field keyword_matched)' },
          max_age_hours: { type: 'integer', description: 'Data lebih lama dari ini dianggap stale', default: 2 },
          project_id: { type: 'string', description: 'Optional — filter ke 1 project tertentu' },
        },
        required: ['keyword'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'trigger_scrape',
      description: 'Scrape keyword dari platform yang ditentukan SECARA PARALEL. Synchronous — return setelah semua platform selesai. Hasilnya otomatis di-persist ke MongoDB.',
      parameters: {
        type: 'object',
        properties: {
          keyword: { type: 'string' },
          platforms: {
            type: 'array',
            items: { type: 'string', enum: ['instagram', 'tiktok', 'facebook', 'youtube', 'twitter', 'threads', 'news'] },
            default: ['instagram', 'tiktok', 'twitter', 'news'],
          },
          max_results_per_platform: { type: 'integer', default: 30 },
          project_id: { type: 'string', description: 'Project untuk attach hasil scrape (optional, default null)' },
        },
        required: ['keyword'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_sentiment_summary',
      description: 'Hitung sentiment positive/neutral/negative untuk posts di keyword tertentu.',
      parameters: {
        type: 'object',
        properties: {
          keyword: { type: 'string' },
          platform: { type: 'string', description: 'Filter optional' },
          project_id: { type: 'string', description: 'Filter project optional' },
        },
        required: ['keyword'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_top_influencers',
      description: 'Top author by total reach (likes+views+comments).',
      parameters: {
        type: 'object',
        properties: {
          keyword: { type: 'string' },
          limit: { type: 'integer', default: 10 },
          project_id: { type: 'string' },
        },
        required: ['keyword'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_trending_news',
      description: 'Berita terbaru tentang keyword dari platform=news.',
      parameters: {
        type: 'object',
        properties: {
          keyword: { type: 'string' },
          limit: { type: 'integer', default: 5 },
          project_id: { type: 'string' },
        },
        required: ['keyword'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_viral_posts',
      description: 'Top posts by engagement (likes+comments+views+shares).',
      parameters: {
        type: 'object',
        properties: {
          keyword: { type: 'string' },
          limit: { type: 'integer', default: 5 },
          platform: { type: 'string' },
          project_id: { type: 'string' },
        },
        required: ['keyword'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_mentions_feed',
      description: 'Raw feed of recent posts/mentions for keyword. Gunakan untuk monitor "Mentions".',
      parameters: {
        type: 'object',
        properties: {
          keyword: { type: 'string' },
          limit: { type: 'integer', default: 20 },
          project_id: { type: 'string' },
        },
        required: ['keyword'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_sna_data',
      description: 'Social Network Analysis — return nodes (authors) + edges (interactions/co-mentions) untuk keyword. Digunakan oleh Monitor 4 (SNA Graph).',
      parameters: {
        type: 'object',
        properties: {
          keyword: { type: 'string' },
          limit: { type: 'integer', default: 50 },
          project_id: { type: 'string' },
        },
        required: ['keyword'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'broadcast_to_monitor',
      description: 'Push view & data ke monitor command center (1-6) lewat WebSocket. Setiap monitor subscribe sesuai monitor_id-nya.',
      parameters: {
        type: 'object',
        properties: {
          monitor_id: { type: 'integer', minimum: 1, maximum: 6 },
          view: {
            type: 'string',
            enum: ['dashboard', 'sentiment', 'mentions', 'sna', 'influencers', 'ai-chat', 'news', 'viral'],
          },
          payload: { type: 'object', description: 'Data yang akan ditampilkan' },
        },
        required: ['monitor_id', 'view', 'payload'],
      },
    },
  },

  // ────────────────────────────────────────────────────────────────────
  // BARU — JARVIS edition
  // ────────────────────────────────────────────────────────────────────
  {
    type: 'function',
    function: {
      name: 'get_locations_breakdown',
      description: 'Agregasi mention berdasarkan kota (field cities di posts). Return top kota beserta jumlah mention dan dominant sentiment per kota. Gunakan untuk jawab "dari daerah mana aja?".',
      parameters: {
        type: 'object',
        properties: {
          keyword: { type: 'string' },
          limit: { type: 'integer', default: 10 },
          project_id: { type: 'string' },
        },
        required: ['keyword'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_negative_samples',
      description: 'Return sample post dengan sentiment=negative beserta content + author + url. Gunakan untuk jawab "negatifnya apa?" / "ada keluhan apa aja?".',
      parameters: {
        type: 'object',
        properties: {
          keyword: { type: 'string' },
          limit: { type: 'integer', default: 10 },
          project_id: { type: 'string' },
          platform: { type: 'string', description: 'Filter optional' },
        },
        required: ['keyword'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_engagement_recommendations',
      description: 'Generate 3-5 saran konkret untuk meningkatkan engagement berdasarkan analisis viral posts + top influencer + sentiment trend. Internal: kompres data + minta LLM kasih saran. Gunakan saat user tanya "apa yang harus dilakukan untuk lebih banyak engagement?".',
      parameters: {
        type: 'object',
        properties: {
          keyword: { type: 'string' },
          project_id: { type: 'string' },
        },
        required: ['keyword'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_daily_summary',
      description: 'Ringkasan 24 jam vs 24 jam sebelumnya — total mention, sentiment shift, top post baru, perubahan signifikan. Gunakan saat user minta "ringkasan harian" / "performa hari ini".',
      parameters: {
        type: 'object',
        properties: {
          keyword: { type: 'string' },
          project_id: { type: 'string' },
        },
        required: ['keyword'],
      },
    },
  },
];

// ========================================================================
// TOOL IMPLEMENTATIONS
// ========================================================================

function buildKeywordFilter(keyword, project_id) {
  const filter = {
    keyword_matched: { $regex: keyword, $options: 'i' }
  };
  if (project_id) filter.project_id = project_id;
  return filter;
}

async function searchExistingPosts({ keyword, max_age_hours = 2, project_id }, { db }) {
  const since = new Date(Date.now() - max_age_hours * 3600 * 1000);
  const baseFilter = buildKeywordFilter(keyword, project_id);

  const totalCount = await db.collection('posts').countDocuments(baseFilter);
  const recentCount = await db.collection('posts').countDocuments({
    ...baseFilter,
    created_at: { $gte: since },
  });
  const lastPost = await db.collection('posts').findOne(
    baseFilter,
    { sort: { created_at: -1 } }
  );

  const byPlatform = await db.collection('posts').aggregate([
    { $match: baseFilter },
    { $group: { _id: '$platform', count: { $sum: 1 } } },
  ]).toArray();
  const platforms = {};
  for (const row of byPlatform) {
    if (row._id) platforms[row._id] = row.count;
  }

  return {
    keyword,
    project_id: project_id || null,
    total_in_db: totalCount,
    recent_count: recentCount,
    is_stale: recentCount === 0,
    last_scraped_at: lastPost?.created_at || null,
    platforms,
  };
}

// PARALLEL trigger_scrape — Promise.all bukan for-of-await
async function triggerScrape(
  { keyword, platforms = ['instagram', 'tiktok', 'twitter', 'news'], max_results_per_platform = 30, project_id },
  ctx
) {
  const tasks = platforms.map(async (platform) => {
    try {
      const data = await ctx.scrapeViaScraperPool(platform, keyword, max_results_per_platform);

      const rawPosts = data?.posts || [];
      let inserted = 0;
      let skipped = 0;

      for (const p of rawPosts) {
        if (p.external_id) {
          const existing = await ctx.db.collection('posts').findOne({
            project_id: project_id || null,
            external_id: p.external_id,
          });
          if (existing) { skipped++; continue; }
        }

        await ctx.db.collection('posts').insertOne({
          project_id: project_id || null,
          external_id: p.external_id,
          platform: p.platform || platform,
          keyword_matched: p.keyword_matched || keyword,
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
          created_at: new Date(),
        });
        inserted++;
      }

      if (ctx.wsHub) {
        ctx.wsHub.broadcastAll({
          type: 'scrape_progress',
          keyword, platform, status: 'completed', inserted,
        });
      }

      return [platform, { count: rawPosts.length, inserted, skipped_dup: skipped }];
    } catch (e) {
      if (ctx.wsHub) {
        ctx.wsHub.broadcastAll({
          type: 'scrape_progress',
          keyword, platform, status: 'failed', error: (e.message || '').slice(0, 200),
        });
      }
      return [platform, { error: e.message }];
    }
  });

  const settled = await Promise.all(tasks);
  const results = Object.fromEntries(settled);
  return { keyword, project_id: project_id || null, platforms_scraped: results };
}

async function getSentimentSummary({ keyword, platform, project_id }, { db }) {
  const filter = buildKeywordFilter(keyword, project_id);
  if (platform) filter.platform = platform;

  const agg = await db.collection('posts').aggregate([
    { $match: filter },
    { $group: { _id: '$sentiment', count: { $sum: 1 } } },
  ]).toArray();

  const result = { positive: 0, neutral: 0, negative: 0 };
  for (const row of agg) {
    if (result.hasOwnProperty(row._id)) result[row._id] = row.count;
    else result.neutral += row.count;
  }
  const total = result.positive + result.neutral + result.negative;

  return {
    keyword,
    platform: platform || 'all',
    total,
    counts: result,
    percentages: {
      positive: total ? +(result.positive / total * 100).toFixed(1) : 0,
      neutral: total ? +(result.neutral / total * 100).toFixed(1) : 0,
      negative: total ? +(result.negative / total * 100).toFixed(1) : 0,
    },
  };
}

async function getTopInfluencers({ keyword, limit = 10, project_id }, { db }) {
  const filter = buildKeywordFilter(keyword, project_id);
  const pipeline = [
    { $match: filter },
    { $group: {
      _id: { author: '$author', handle: '$handle' },
      platforms: { $addToSet: '$platform' },
      post_count: { $sum: 1 },
      total_likes: { $sum: { $ifNull: ['$likes', 0] } },
      total_views: { $sum: { $ifNull: ['$views', 0] } },
      total_comments: { $sum: { $ifNull: ['$comments', 0] } },
      total_shares: { $sum: { $ifNull: ['$shares', 0] } },
      avatar: { $first: '$avatar' },
    }},
    { $addFields: {
      total_reach: { $add: ['$total_likes', '$total_views', '$total_comments'] },
    }},
    { $sort: { total_reach: -1 } },
    { $limit: limit },
    { $project: {
      _id: 0,
      author: '$_id.author',
      handle: '$_id.handle',
      platforms: 1, post_count: 1,
      total_likes: 1, total_views: 1, total_comments: 1, total_shares: 1,
      total_reach: 1, avatar: 1,
    }},
  ];
  return await db.collection('posts').aggregate(pipeline).toArray();
}

async function getTrendingNews({ keyword, limit = 5, project_id }, { db }) {
  const filter = { ...buildKeywordFilter(keyword, project_id), platform: 'news' };
  return await db.collection('posts')
    .find(filter)
    .sort({ post_date: -1, created_at: -1 })
    .limit(limit)
    .project({ author: 1, content: 1, url: 1, source_name: 1, post_date: 1, sentiment: 1 })
    .toArray();
}

async function getViralPosts({ keyword, limit = 5, platform, project_id }, { db }) {
  const filter = buildKeywordFilter(keyword, project_id);
  if (platform) filter.platform = platform;
  const pipeline = [
    { $match: filter },
    { $addFields: {
      engagement: { $add: [
        { $ifNull: ['$likes', 0] },
        { $ifNull: ['$comments', 0] },
        { $ifNull: ['$views', 0] },
        { $ifNull: ['$shares', 0] },
      ]}
    }},
    { $sort: { engagement: -1 } },
    { $limit: limit },
    { $project: {
      _id: 0, platform: 1, author: 1, handle: 1, content: 1, url: 1,
      likes: 1, views: 1, comments: 1, shares: 1, engagement: 1,
      sentiment: 1, post_date: 1, avatar: 1,
    }},
  ];
  return await db.collection('posts').aggregate(pipeline).toArray();
}

async function getMentionsFeed({ keyword, limit = 20, project_id }, { db }) {
  const filter = buildKeywordFilter(keyword, project_id);
  return await db.collection('posts')
    .find(filter)
    .sort({ post_date: -1, created_at: -1 })
    .limit(limit)
    .project({
      _id: 0, platform: 1, author: 1, handle: 1, avatar: 1,
      content: 1, url: 1, sentiment: 1, post_date: 1,
      likes: 1, comments: 1, views: 1,
    })
    .toArray();
}

async function getSnaData({ keyword, limit = 50, project_id }, { db }) {
  const filter = buildKeywordFilter(keyword, project_id);
  const posts = await db.collection('posts')
    .find(filter)
    .limit(500)
    .project({ author: 1, handle: 1, platform: 1, hashtags: 1, likes: 1, views: 1, comments: 1 })
    .toArray();

  const authorMap = new Map();
  for (const p of posts) {
    if (!p.author) continue;
    const existing = authorMap.get(p.author) || {
      author: p.author, handle: p.handle, platforms: new Set(),
      post_count: 0, total_reach: 0, hashtags: new Set(),
    };
    existing.platforms.add(p.platform);
    existing.post_count++;
    existing.total_reach += (p.likes || 0) + (p.views || 0) + (p.comments || 0);
    let tags = [];
    try { tags = typeof p.hashtags === 'string' ? JSON.parse(p.hashtags) : (p.hashtags || []); } catch {}
    if (Array.isArray(tags)) tags.forEach(t => existing.hashtags.add(String(t).toLowerCase()));
    authorMap.set(p.author, existing);
  }

  const nodes = Array.from(authorMap.values())
    .sort((a, b) => b.total_reach - a.total_reach)
    .slice(0, limit)
    .map(n => ({
      id: n.author, label: n.author, handle: n.handle,
      platforms: Array.from(n.platforms),
      post_count: n.post_count, total_reach: n.total_reach,
      hashtag_count: n.hashtags.size,
    }));

  const edges = [];
  const filteredAuthors = nodes.map(n => ({ id: n.id, hashtags: authorMap.get(n.id).hashtags }));
  for (let i = 0; i < filteredAuthors.length; i++) {
    for (let j = i + 1; j < filteredAuthors.length; j++) {
      const a = filteredAuthors[i], b = filteredAuthors[j];
      let shared = 0;
      for (const h of a.hashtags) if (b.hashtags.has(h)) shared++;
      if (shared > 0) {
        edges.push({ source: a.id, target: b.id, weight: shared });
      }
    }
  }

  return { keyword, nodes, edges, total_posts_analyzed: posts.length };
}

async function broadcastToMonitor({ monitor_id, view, payload }, { wsHub }) {
  if (!wsHub) {
    return { error: 'wsHub_unavailable', monitor_id, view, broadcasted: false };
  }
  const sent = wsHub.broadcast(monitor_id, { type: 'render', view, payload, timestamp: Date.now() });
  return { monitor_id, view, broadcasted: true, subscribers_notified: sent };
}

// ────────────────────────────────────────────────────────────────────
// BARU — JARVIS edition tools
// ────────────────────────────────────────────────────────────────────

async function getLocationsBreakdown({ keyword, limit = 10, project_id }, { db }) {
  const filter = buildKeywordFilter(keyword, project_id);
  const posts = await db.collection('posts')
    .find(filter)
    .project({ cities: 1, sentiment: 1 })
    .toArray();

  // cities di-store sebagai JSON string atau array
  const cityMap = new Map(); // city -> {count, positive, neutral, negative}
  for (const p of posts) {
    let cities = [];
    try {
      cities = typeof p.cities === 'string' ? JSON.parse(p.cities) : (p.cities || []);
    } catch { cities = []; }
    if (!Array.isArray(cities)) continue;

    for (const city of cities) {
      const c = String(city || '').trim();
      if (!c) continue;
      const e = cityMap.get(c) || { city: c, count: 0, positive: 0, neutral: 0, negative: 0 };
      e.count++;
      if (e.hasOwnProperty(p.sentiment)) e[p.sentiment]++;
      else e.neutral++;
      cityMap.set(c, e);
    }
  }

  const top = Array.from(cityMap.values())
    .sort((a, b) => b.count - a.count)
    .slice(0, limit)
    .map(c => {
      const total = c.count;
      const dominantSentiment =
        c.negative > c.positive && c.negative > c.neutral ? 'negative' :
        c.positive > c.negative && c.positive > c.neutral ? 'positive' : 'neutral';
      return {
        city: c.city,
        mention_count: c.count,
        dominant_sentiment: dominantSentiment,
        sentiment_breakdown: {
          positive: c.positive, neutral: c.neutral, negative: c.negative,
          negative_pct: total ? +(c.negative / total * 100).toFixed(1) : 0,
        },
      };
    });

  return {
    keyword,
    total_posts_with_location: posts.filter(p => {
      try { const c = typeof p.cities === 'string' ? JSON.parse(p.cities) : (p.cities || []); return Array.isArray(c) && c.length > 0; }
      catch { return false; }
    }).length,
    total_unique_cities: cityMap.size,
    top_locations: top,
  };
}

async function getNegativeSamples({ keyword, limit = 10, project_id, platform }, { db }) {
  const filter = { ...buildKeywordFilter(keyword, project_id), sentiment: 'negative' };
  if (platform) filter.platform = platform;

  const samples = await db.collection('posts')
    .find(filter)
    .sort({ post_date: -1, created_at: -1 })
    .limit(limit)
    .project({
      _id: 0, platform: 1, author: 1, handle: 1, content: 1, url: 1,
      likes: 1, comments: 1, views: 1, post_date: 1, source_name: 1,
    })
    .toArray();

  const totalNegative = await db.collection('posts').countDocuments(filter);

  return {
    keyword,
    total_negative_in_db: totalNegative,
    samples_returned: samples.length,
    samples,
  };
}

async function getEngagementRecommendations({ keyword, project_id }, ctx) {
  // Kompres data konteks ke LLM untuk minta saran konkret
  const [sentiment, topInfluencers, viralPosts, locationsBreakdown] = await Promise.all([
    getSentimentSummary({ keyword, project_id }, ctx),
    getTopInfluencers({ keyword, limit: 5, project_id }, ctx),
    getViralPosts({ keyword, limit: 5, project_id }, ctx),
    getLocationsBreakdown({ keyword, limit: 5, project_id }, ctx),
  ]);

  // Sub-call ke LLM (bukan tool calling — plain chat completion)
  const subPrompt = `Berdasarkan data social media monitoring untuk keyword "${keyword}":

SENTIMENT: ${sentiment.percentages.positive}% positif / ${sentiment.percentages.neutral}% netral / ${sentiment.percentages.negative}% negatif (total ${sentiment.total} mention)

TOP 5 INFLUENCER:
${topInfluencers.map((i, idx) => `${idx+1}. ${i.author || i.handle} — ${i.platforms?.join(',')} — reach ${i.total_reach}`).join('\n')}

TOP 5 VIRAL POSTS:
${viralPosts.map((v, idx) => `${idx+1}. [${v.platform}] @${v.handle || v.author} — engagement ${v.engagement} — sentiment ${v.sentiment} — "${(v.content || '').slice(0, 100)}"`).join('\n')}

TOP 5 LOKASI:
${locationsBreakdown.top_locations.map((l, idx) => `${idx+1}. ${l.city} — ${l.mention_count} mention, dominant ${l.dominant_sentiment}`).join('\n') || '(no location data)'}

Beri 3-5 saran ACTIONABLE dan KONKRET untuk meningkatkan engagement. Format: nomor + 1 kalimat saran + 1 kalimat alasan/data pendukung. Bahasa Indonesia, langsung tanpa basa-basi.`;

  const apiKey = config.get('openrouter_api_key');
  if (!apiKey) {
    return {
      keyword,
      recommendations_text: 'OpenRouter API key tidak diset, tidak bisa generate saran.',
      raw_context: { sentiment, topInfluencers, viralPosts, locationsBreakdown },
    };
  }

  try {
    const resp = await fetch('https://openrouter.ai/api/v1/chat/completions', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
        'HTTP-Referer': config.get('openrouter_referer'),
        'X-Title': 'Slaytics Engagement Recommender',
      },
      body: JSON.stringify({
        model: config.get('openrouter_model'),
        messages: [
          { role: 'system', content: 'Kamu adalah social media strategist senior. Beri saran tajam, konkret, data-driven. Tidak bertele-tele.' },
          { role: 'user', content: subPrompt },
        ],
        temperature: 0.5,
        max_tokens: 600,
      }),
    });
    const data = await resp.json();
    const text = data.choices?.[0]?.message?.content || '(no response)';
    return {
      keyword,
      recommendations_text: text,
      based_on: {
        total_mentions: sentiment.total,
        sentiment_pct: sentiment.percentages,
        top_influencer_count: topInfluencers.length,
        viral_post_count: viralPosts.length,
        unique_cities: locationsBreakdown.total_unique_cities,
      },
    };
  } catch (e) {
    return { keyword, error: e.message, raw_context: { sentiment, topInfluencers } };
  }
}

async function getDailySummary({ keyword, project_id }, { db }) {
  const filter = buildKeywordFilter(keyword, project_id);
  const now = new Date();
  const yesterday = new Date(now.getTime() - 24 * 3600 * 1000);
  const dayBefore = new Date(now.getTime() - 48 * 3600 * 1000);

  const [today, yest] = await Promise.all([
    db.collection('posts').aggregate([
      { $match: { ...filter, $or: [{ post_date: { $gte: yesterday } }, { created_at: { $gte: yesterday } }] } },
      { $group: { _id: '$sentiment', count: { $sum: 1 }, eng: { $sum: { $add: [
        { $ifNull: ['$likes', 0] }, { $ifNull: ['$comments', 0] },
        { $ifNull: ['$views', 0] }, { $ifNull: ['$shares', 0] }
      ]}}}}
    ]).toArray(),
    db.collection('posts').aggregate([
      { $match: { ...filter, $or: [
        { post_date: { $gte: dayBefore, $lt: yesterday } },
        { created_at: { $gte: dayBefore, $lt: yesterday } }
      ]}},
      { $group: { _id: '$sentiment', count: { $sum: 1 } }}
    ]).toArray(),
  ]);

  function summarize(rows) {
    const out = { positive: 0, neutral: 0, negative: 0, eng: 0 };
    for (const r of rows) {
      if (out.hasOwnProperty(r._id)) out[r._id] = r.count;
      else out.neutral += r.count;
      out.eng += r.eng || 0;
    }
    out.total = out.positive + out.neutral + out.negative;
    return out;
  }
  const t = summarize(today);
  const y = summarize(yest);

  function delta(a, b) {
    if (b === 0) return a === 0 ? 0 : 100;
    return +(((a - b) / b) * 100).toFixed(1);
  }

  // Top viral 1 post hari ini
  const topViral = await db.collection('posts').aggregate([
    { $match: { ...filter, $or: [{ post_date: { $gte: yesterday } }, { created_at: { $gte: yesterday } }] }},
    { $addFields: { engagement: { $add: [
      { $ifNull: ['$likes', 0] }, { $ifNull: ['$comments', 0] },
      { $ifNull: ['$views', 0] }, { $ifNull: ['$shares', 0] }
    ]}}},
    { $sort: { engagement: -1 } },
    { $limit: 3 },
    { $project: { _id: 0, platform: 1, author: 1, handle: 1, content: 1, url: 1,
                  engagement: 1, sentiment: 1, post_date: 1 }},
  ]).toArray();

  return {
    keyword,
    period: '24h vs 24-48h',
    today: { total: t.total, sentiment: { positive: t.positive, neutral: t.neutral, negative: t.negative },
             total_engagement: t.eng },
    yesterday: { total: y.total, sentiment: { positive: y.positive, neutral: y.neutral, negative: y.negative }},
    delta_pct: {
      total_mentions: delta(t.total, y.total),
      negative_count: delta(t.negative, y.negative),
      positive_count: delta(t.positive, y.positive),
    },
    alert_flags: {
      negative_spike: t.negative > y.negative * 1.5 && t.negative > 10,
      volume_spike: t.total > y.total * 2 && t.total > 30,
      volume_drop: y.total > 30 && t.total < y.total * 0.5,
    },
    top_3_viral_today: topViral,
  };
}

const TOOL_IMPLS = {
  search_existing_posts: searchExistingPosts,
  trigger_scrape: triggerScrape,
  get_sentiment_summary: getSentimentSummary,
  get_top_influencers: getTopInfluencers,
  get_trending_news: getTrendingNews,
  get_viral_posts: getViralPosts,
  get_mentions_feed: getMentionsFeed,
  get_sna_data: getSnaData,
  broadcast_to_monitor: broadcastToMonitor,
  get_locations_breakdown: getLocationsBreakdown,
  get_negative_samples: getNegativeSamples,
  get_engagement_recommendations: getEngagementRecommendations,
  get_daily_summary: getDailySummary,
};

// ========================================================================
// SYSTEM PROMPT — JARVIS persona
// ========================================================================
const SYSTEM_PROMPT = `Kamu adalah JARVIS untuk Slaytics — assistant pribadi command center monitoring social media. Panggil user dengan "Bos". Sopan, ringkas, tidak bertele-tele, langsung to-the-point. Selalu actionable.

Layout 6 monitor:
- Monitor 1: Dashboard (overview metrics)
- Monitor 2: Sentiment analysis chart
- Monitor 3: Mentions feed (raw posts terbaru)
- Monitor 4: SNA graph (network of authors)
- Monitor 5: Top Influencers leaderboard
- Monitor 6: AI Chat (input pengguna) + News + Viral content

Workflow standar untuk command "tampilkan <keyword>" / "monitor <keyword>" / "analisis <keyword>":
1. Call search_existing_posts dulu untuk cek data terkini.
2. Kalau is_stale=true ATAU recent_count < 5, call trigger_scrape (default platforms: instagram, tiktok, twitter, news). Threads & Facebook & YouTube opt-in — call kalau user explicit minta atau ada konteks (kasus krisis, monitoring viral, atau platform itu memang relevan).
3. Setelah data ready, call PARALEL: get_sentiment_summary, get_top_influencers, get_trending_news, get_viral_posts, get_mentions_feed, get_sna_data, get_locations_breakdown, get_negative_samples, get_daily_summary.
4. Broadcast ke monitor sesuai view-nya:
   - Monitor 1 (dashboard): payload gabungan {sentiment, total_mentions, top_3_influencers, viral_top_1, locations_breakdown, daily_delta}
   - Monitor 2 (sentiment): payload sentiment summary
   - Monitor 3 (mentions): payload mentions feed
   - Monitor 4 (sna): payload sna data
   - Monitor 5 (influencers): payload top influencers
   - Monitor 6 (news + viral): payload {news, viral}
5. Kasih response final yang akan DIBACAKAN OLEH TTS — jadi format khusus:

   FORMAT RESPONSE FINAL (penting, ini akan disuarakan):
   - Mulai dengan satu kalimat ringkas berisi angka kunci. Contoh: "Bos, total mention msglow 24 jam terakhir 1.247, naik 38% dari kemarin."
   - Sebut sentiment dominan + apakah ada lonjakan negatif. Contoh: "Sentimen 62% positif, tapi ada spike negatif di TikTok 18%."
   - Sebut top 1 lokasi + top 1 viral post. Contoh: "Mention terbanyak dari Jakarta dan Surabaya. Post paling viral: video TikTok @userX dengan 47 ribu engagement."
   - Tutup dengan 2 saran actionable. Contoh: "Saran saya: kontak influencer @nameY untuk amplifikasi positif, dan rilis statement counter terhadap keluhan kemasan yang muncul dari Bandung."

   JANGAN pakai markdown bullet/heading di response final — TTS akan baca apa adanya. Pisahkan dengan koma dan titik saja. Maksimal 5 kalimat.

Aturan tambahan:
- Kalau user cuma minta 1 metric ("berapa mention X hari ini?"), gak perlu broadcast — cukup answer langsung 1 kalimat.
- Kalau user tanya "ada negatif apa?" → call get_negative_samples + ringkas 3-4 keluhan utama dalam 2 kalimat.
- Kalau user tanya "dari daerah mana?" → call get_locations_breakdown + sebut top 3 kota.
- Kalau user tanya "apa yang harus dilakukan untuk engagement lebih banyak?" → call get_engagement_recommendations.
- Kalau user minta "ringkasan harian" / "briefing pagi" → call get_daily_summary.
- Kalau ada anomali (sentiment negative > 30% atau spike volume > 3x), warn explicitly di awal response: "Bos, perhatian — ada alert..."
- Kalau user chit-chat (sapaan, terima kasih), respon ramah singkat tanpa call tool.
- Bahasa: ikuti user. Default Indonesia.`;

// ========================================================================
// MAIN AGENT LOOP
// ========================================================================
async function runAgent(userCommand, ctx, opts = {}) {
  const apiKey = config.get('openrouter_api_key');
  const model = opts.model || config.get('openrouter_model');
  const maxIter = opts.maxIterations || config.get('agent_max_iterations');

  if (!apiKey) {
    throw new Error('OPENROUTER_API_KEY not configured. Set it in Settings → AI Agent.');
  }

  const messages = [
    { role: 'system', content: SYSTEM_PROMPT },
    { role: 'user', content: userCommand },
  ];

  const trace = [];

  for (let i = 0; i < maxIter; i++) {
    const startCall = Date.now();

    let resp;
    try {
      resp = await fetch('https://openrouter.ai/api/v1/chat/completions', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${apiKey}`,
          'Content-Type': 'application/json',
          'HTTP-Referer': config.get('openrouter_referer'),
          'X-Title': 'Slaytics Command Center',
        },
        body: JSON.stringify({
          model,
          messages,
          tools: TOOLS,
          tool_choice: 'auto',
          temperature: 0.3,
        }),
      });
    } catch (e) {
      throw new Error(`OpenRouter network error: ${e.message}`);
    }

    if (!resp.ok) {
      const txt = await resp.text();
      throw new Error(`OpenRouter HTTP ${resp.status}: ${txt.slice(0, 500)}`);
    }

    const data = await resp.json();
    if (!data.choices?.[0]) {
      throw new Error(`OpenRouter response invalid: ${JSON.stringify(data).slice(0, 300)}`);
    }

    const msg = data.choices[0].message;
    messages.push(msg);

    trace.push({
      iteration: i + 1,
      duration_ms: Date.now() - startCall,
      tool_calls: (msg.tool_calls || []).map(tc => ({
        name: tc.function?.name,
        args: tc.function?.arguments,
      })),
      content_preview: typeof msg.content === 'string' ? msg.content.slice(0, 200) : null,
    });

    if (!msg.tool_calls?.length) {
      return {
        final_message: msg.content || '',
        iterations: i + 1,
        trace,
        usage: data.usage,
      };
    }

    const toolResults = await Promise.all(
      msg.tool_calls.map(async (tc) => {
        const fnName = tc.function.name;
        let args = {};
        try {
          args = JSON.parse(tc.function.arguments || '{}');
        } catch (e) {
          return {
            tool_call_id: tc.id, role: 'tool', name: fnName,
            content: JSON.stringify({ error: 'invalid_json_args', raw: tc.function.arguments }),
          };
        }

        const impl = TOOL_IMPLS[fnName];
        if (!impl) {
          return {
            tool_call_id: tc.id, role: 'tool', name: fnName,
            content: JSON.stringify({ error: 'unknown_tool', tool: fnName }),
          };
        }

        try {
          const result = await impl(args, ctx);
          return {
            tool_call_id: tc.id, role: 'tool', name: fnName,
            content: JSON.stringify(result),
          };
        } catch (e) {
          console.error(`[agent] tool ${fnName} failed:`, e);
          return {
            tool_call_id: tc.id, role: 'tool', name: fnName,
            content: JSON.stringify({ error: e.message || 'tool_execution_failed' }),
          };
        }
      })
    );

    messages.push(...toolResults);
  }

  throw new Error(`Max iterations (${maxIter}) reached without final response`);
}

module.exports = { runAgent, TOOLS, TOOL_IMPLS };
