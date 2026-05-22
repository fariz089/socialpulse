// backend/alerts.js
// Anomaly detection untuk Slaytics — deteksi sentimen jelek, lonjakan volume,
// viral velocity, dan keyword trigger crisis. Dipanggil oleh scheduler.js setelah
// tiap scrape selesai.
//
// Filosofi: prefer false positive ringan daripada miss real crisis.
// Tapi alert dispatch hanya untuk severity high/critical supaya tidak spam.

const { dispatchAlert } = require('./notifier');
const config = require('./config');

// Threshold dibaca dari config (DB → env). Bisa diubah lewat UI tanpa restart.
// Min posts untuk fire sentiment alert (anti false positive volume rendah)
const MIN_POSTS_FOR_SENTIMENT_ALERT = 30;
const VIRAL_VELOCITY_MULTIPLIER = 5;  // post engagement > avg * X dalam 1 jam pertama

// Default crisis keywords (bisa di-override per project)
const DEFAULT_CRISIS_KEYWORDS = [
  'scam', 'penipuan', 'penipu', 'tipu', 'bohong', 'bohongin',
  'boikot', 'boycott',
  'kecewa', 'mengecewakan',
  'racun', 'beracun', 'keracunan',
  'palsu', 'kw', 'tiruan',
  'jelek banget', 'parah banget',
  'lapor polisi', 'tuntutan', 'gugat',
  'rusak', 'cacat',
];

// ─── Helper: cek cooldown ───────────────────────────────────────────
async function isInCooldown(db, keyword, project_id, alert_type) {
  const cooldownMin = config.get('alert_cooldown_minutes');
  const since = new Date(Date.now() - cooldownMin * 60 * 1000);
  const recent = await db.collection('alerts').findOne({
    keyword,
    project_id: project_id || null,
    type: alert_type,
    detected_at: { $gte: since },
  });
  return !!recent;
}

// ─── Helper: ambil baseline 7 hari terakhir untuk keyword ───────────
async function getBaseline(db, keyword, project_id) {
  const oneDayAgo = new Date(Date.now() - 24 * 3600 * 1000);
  const sevenDaysAgo = new Date(Date.now() - 7 * 24 * 3600 * 1000);

  const filter = {
    keyword_matched: { $regex: keyword, $options: 'i' },
    created_at: { $gte: sevenDaysAgo, $lt: oneDayAgo },
  };
  if (project_id) filter.project_id = project_id;

  // Hitung rata-rata posts per 24 jam selama 6 hari pre-yesterday
  const totalCount = await db.collection('posts').countDocuments(filter);
  const avgPostsPer24h = totalCount / 6;

  // Sentiment baseline
  const sentAgg = await db.collection('posts').aggregate([
    { $match: filter },
    { $group: { _id: '$sentiment', count: { $sum: 1 } } },
  ]).toArray();
  const baseline_negative_pct = totalCount > 0
    ? (sentAgg.find(s => s._id === 'negative')?.count || 0) / totalCount * 100
    : 0;

  return {
    avg_posts_per_24h: avgPostsPer24h,
    baseline_negative_pct,
    sample_size: totalCount,
  };
}

// ─── Detection 1: sentimen negatif spike ────────────────────────────
async function detectNegativeSentimentSpike(db, keyword, project_id) {
  const oneHourAgo = new Date(Date.now() - 3600 * 1000);
  const filter = {
    keyword_matched: { $regex: keyword, $options: 'i' },
    created_at: { $gte: oneHourAgo },
  };
  if (project_id) filter.project_id = project_id;

  const recent = await db.collection('posts').find(filter).toArray();
  if (recent.length < MIN_POSTS_FOR_SENTIMENT_ALERT) return null;

  const negCount = recent.filter(p => p.sentiment === 'negative').length;
  const negPct = (negCount / recent.length) * 100;
  const baseline = await getBaseline(db, keyword, project_id);
  const negativeThreshold = config.get('alert_negative_threshold');

  // Trigger: negPct di atas threshold absolut DAN > baseline + 15%
  if (negPct < negativeThreshold) return null;
  if (baseline.sample_size > 50 && negPct < baseline.baseline_negative_pct + 15) return null;

  // Sample 3 post negatif terburuk (paling banyak engagement)
  const samples = recent
    .filter(p => p.sentiment === 'negative')
    .sort((a, b) => ((b.likes || 0) + (b.comments || 0) + (b.views || 0)) - ((a.likes || 0) + (a.comments || 0) + (a.views || 0)))
    .slice(0, 3)
    .map(p => ({
      author: p.author, handle: p.handle, content: p.content,
      url: p.url, platform: p.platform,
      likes: p.likes, comments: p.comments, views: p.views,
    }));

  return {
    type: 'negative_sentiment_spike',
    severity: negPct > 50 ? 'critical' : 'high',
    keyword,
    project_id,
    message: `Sentimen negatif ${negPct.toFixed(0)}% dari ${recent.length} post terakhir 1 jam (baseline ${baseline.baseline_negative_pct.toFixed(0)}%). Perlu dicek sebelum viral.`,
    metrics: {
      negative_pct: +negPct.toFixed(1),
      negative_count: negCount,
      total_recent: recent.length,
      baseline_pct: +baseline.baseline_negative_pct.toFixed(1),
    },
    samples,
    recommended_action: 'Buka Monitor 3 (Mentions) untuk lihat content lengkap. Pertimbangkan: respons resmi, klarifikasi, atau eskalasi ke PR team.',
    detected_at: new Date(),
  };
}

// ─── Detection 2: volume spike ──────────────────────────────────────
async function detectVolumeSpike(db, keyword, project_id) {
  const oneHourAgo = new Date(Date.now() - 3600 * 1000);
  const filter = {
    keyword_matched: { $regex: keyword, $options: 'i' },
    created_at: { $gte: oneHourAgo },
  };
  if (project_id) filter.project_id = project_id;

  const recentCount = await db.collection('posts').countDocuments(filter);
  if (recentCount < 50) return null;  // ignore early signal

  const baseline = await getBaseline(db, keyword, project_id);
  if (baseline.sample_size < 100) return null;  // not enough history

  const expectedPerHour = baseline.avg_posts_per_24h / 24;
  if (expectedPerHour < 1) return null;  // baseline too low to compare meaningfully

  const ratio = recentCount / expectedPerHour;
  const volumeMultiplier = config.get('alert_volume_multiplier');
  if (ratio < volumeMultiplier) return null;

  // Cek mayoritas sentimen — kalau positif, ini opportunity bukan threat
  const sentAgg = await db.collection('posts').aggregate([
    { $match: filter },
    { $group: { _id: '$sentiment', count: { $sum: 1 } } },
  ]).toArray();
  const sentByType = {};
  for (const s of sentAgg) sentByType[s._id || 'neutral'] = s.count;
  const dominantSentiment = Object.entries(sentByType).sort((a, b) => b[1] - a[1])[0]?.[0] || 'neutral';

  const isCrisis = dominantSentiment === 'negative';

  return {
    type: 'volume_spike',
    severity: isCrisis ? 'critical' : ratio > 10 ? 'high' : 'medium',
    keyword,
    project_id,
    message: `Volume mention ${recentCount} dalam 1 jam, ${ratio.toFixed(1)}x baseline (${expectedPerHour.toFixed(1)}/jam). Sentimen dominan: ${dominantSentiment}.`,
    metrics: {
      recent_count: recentCount,
      baseline_per_hour: +expectedPerHour.toFixed(1),
      multiplier: +ratio.toFixed(1),
      dominant_sentiment: dominantSentiment,
    },
    recommended_action: isCrisis
      ? 'Crisis kemungkinan. Eskalasi sekarang. Buka semua monitor untuk full picture.'
      : 'Mungkin opportunity (positive viral). Cek konten viral untuk amplify.',
    detected_at: new Date(),
  };
}

// ─── Detection 3: viral velocity (single post yang naik cepat) ──────
async function detectViralVelocity(db, keyword, project_id) {
  const sixHoursAgo = new Date(Date.now() - 6 * 3600 * 1000);
  const filter = {
    keyword_matched: { $regex: keyword, $options: 'i' },
    post_date: { $gte: sixHoursAgo },
  };
  if (project_id) filter.project_id = project_id;

  // Hitung average engagement untuk post 6 jam terakhir
  const recent = await db.collection('posts').find(filter).toArray();
  if (recent.length < 20) return null;

  const engagements = recent.map(p => (p.likes || 0) + (p.comments || 0) + (p.views || 0));
  const avgEng = engagements.reduce((a, b) => a + b, 0) / engagements.length;
  if (avgEng < 100) return null;  // baseline too low

  const threshold = avgEng * VIRAL_VELOCITY_MULTIPLIER;

  // Top post in last 6h
  const topPost = recent
    .map(p => ({ ...p, _eng: (p.likes || 0) + (p.comments || 0) + (p.views || 0) }))
    .sort((a, b) => b._eng - a._eng)[0];

  if (!topPost || topPost._eng < threshold) return null;

  const isNegative = topPost.sentiment === 'negative';

  return {
    type: 'viral_velocity',
    severity: isNegative ? 'critical' : 'high',
    keyword,
    project_id,
    message: `Post @${topPost.handle || topPost.author} di ${topPost.platform} engagement ${topPost._eng.toLocaleString('id-ID')} (${(topPost._eng / avgEng).toFixed(1)}x rata-rata). Sentimen: ${topPost.sentiment}.`,
    metrics: {
      post_engagement: topPost._eng,
      avg_engagement: Math.round(avgEng),
      multiplier: +(topPost._eng / avgEng).toFixed(1),
      sentiment: topPost.sentiment,
    },
    samples: [{
      author: topPost.author, handle: topPost.handle, content: topPost.content,
      url: topPost.url, platform: topPost.platform,
      likes: topPost.likes, comments: topPost.comments, views: topPost.views,
    }],
    recommended_action: isNegative
      ? 'POST NEGATIF VIRAL. Pertimbangkan: kontak influencer untuk klarifikasi, atau rilis statement counter sebelum makin spread.'
      : 'Post positif lagi viral. Boost dengan share/repost dari akun resmi untuk amplify reach.',
    detected_at: new Date(),
  };
}

// ─── Detection 4: crisis keyword trigger ────────────────────────────
async function detectCrisisKeyword(db, keyword, project_id, crisis_words = DEFAULT_CRISIS_KEYWORDS) {
  const oneHourAgo = new Date(Date.now() - 3600 * 1000);
  const filter = {
    keyword_matched: { $regex: keyword, $options: 'i' },
    created_at: { $gte: oneHourAgo },
  };
  if (project_id) filter.project_id = project_id;

  const recent = await db.collection('posts').find(filter).toArray();
  if (recent.length === 0) return null;

  // Cari post yang content-nya mengandung crisis keyword
  const wordPattern = new RegExp(`\\b(${crisis_words.join('|')})\\b`, 'i');
  const triggered = recent.filter(p => wordPattern.test(p.content || ''));

  if (triggered.length < 3) return null;  // butuh minimal 3 untuk avoid noise

  // Group by which crisis word triggered
  const wordCounts = {};
  for (const p of triggered) {
    const m = (p.content || '').match(wordPattern);
    if (m) {
      const w = m[1].toLowerCase();
      wordCounts[w] = (wordCounts[w] || 0) + 1;
    }
  }
  const topWords = Object.entries(wordCounts).sort((a, b) => b[1] - a[1]).slice(0, 3);

  const samples = triggered
    .sort((a, b) => ((b.likes || 0) + (b.comments || 0)) - ((a.likes || 0) + (a.comments || 0)))
    .slice(0, 3)
    .map(p => ({
      author: p.author, handle: p.handle, content: p.content,
      url: p.url, platform: p.platform,
      likes: p.likes, comments: p.comments, views: p.views,
    }));

  return {
    type: 'crisis_keyword',
    severity: triggered.length >= 10 ? 'critical' : 'high',
    keyword,
    project_id,
    message: `${triggered.length} post dalam 1 jam mengandung kata krisis: ${topWords.map(([w, c]) => `"${w}" (${c})`).join(', ')}.`,
    metrics: {
      triggered_count: triggered.length,
      total_recent: recent.length,
      top_words: topWords,
    },
    samples,
    recommended_action: 'Review konten satu per satu. Kalau valid concern, respons cepat. Kalau hoax/serangan terkoordinasi, dokumentasi untuk action legal.',
    detected_at: new Date(),
  };
}

// ─── Main: run all detectors untuk satu keyword + dispatch ──────────
async function runAllDetectors(keyword, project_id, ctx) {
  const { db, wsHub } = ctx;
  const detectors = [
    { fn: detectNegativeSentimentSpike, type: 'negative_sentiment_spike' },
    { fn: detectVolumeSpike, type: 'volume_spike' },
    { fn: detectViralVelocity, type: 'viral_velocity' },
    { fn: detectCrisisKeyword, type: 'crisis_keyword' },
  ];

  const fired = [];
  for (const { fn, type } of detectors) {
    try {
      // Cooldown check supaya tidak spam
      if (await isInCooldown(db, keyword, project_id, type)) continue;

      const alert = await fn(db, keyword, project_id);
      if (alert) {
        await dispatchAlert(alert, { wsHub, db });
        fired.push(alert);
      }
    } catch (e) {
      console.error(`[alerts] detector ${type} failed for "${keyword}":`, e.message);
    }
  }

  return fired;
}

module.exports = {
  runAllDetectors,
  detectNegativeSentimentSpike,
  detectVolumeSpike,
  detectViralVelocity,
  detectCrisisKeyword,
  getBaseline,
  DEFAULT_CRISIS_KEYWORDS,
};
