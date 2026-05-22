// backend/scheduler.js
// 24/7 continuous monitoring loop. Tiap interval (default 60 menit), loop semua
// project aktif → scrape semua keyword × platform → run alert detectors →
// dispatch ke Telegram/Discord/WS kalau ada anomali.
//
// Tidak butuh node-cron — pakai setInterval supaya tidak nambah dependency.
// Hot-reload: kalau config interval berubah lewat UI, scheduler restart pakai value baru.

const { runAllDetectors } = require('./alerts');
const { sendDailyBriefing } = require('./notifier');
const config = require('./config');

// State
let _running = false;       // true saat 1 cycle sedang jalan (avoid overlap)
let _interval = null;
let _briefingInterval = null;
let _watcher = null;        // interval untuk watch config changes
let _currentIntervalMin = null;
let _ctxRef = null;
let _lastRunAt = null;
let _lastResults = null;

// ─── Core: 1 cycle of monitoring ─────────────────────────────────────
async function runCycle(ctx) {
  if (_running) {
    console.log('[scheduler] previous cycle still running, skip this tick');
    return;
  }
  _running = true;
  const startedAt = Date.now();

  try {
    const { db, scrapeViaScraperPool, wsHub } = ctx;

    // Ambil semua project yang aktif (kalau punya field `monitoring_enabled`,
    // hanya proses yang true. Default: semua project).
    const projects = await db.collection('projects').find({
      $or: [
        { monitoring_enabled: { $ne: false } },
        { monitoring_enabled: { $exists: false } },
      ],
    }).toArray();

    console.log(`[scheduler] cycle start — ${projects.length} project(s) to monitor`);

    const cycleResults = {
      started_at: new Date(startedAt),
      projects: [],
      total_alerts: 0,
    };

    for (const project of projects) {
      const projectResult = {
        project_id: String(project._id),
        project_name: project.name,
        keywords_scraped: 0,
        platforms_scraped: 0,
        alerts: [],
      };

      // Determine keywords + platforms dari project.
      // Default include semua main platforms (kecuali youtube yg butuh per-video setup).
      // Project yg explicit set platforms (via UI) akan respect pilihan user, gak auto-tambah.
      //
      // Twitter & Threads sengaja OPTIONAL — project lama gak punya pilihan ini di
      // DB-nya, jadi kita gak mau auto-scrape platform yang akunnya belum di-setup.
      // User yang explicit pilih di UI (project.platforms include 'twitter'/'threads')
      // baru di-include.
      const keywords = (project.keywords || []).filter(Boolean);
      const platforms = (project.platforms || ['instagram', 'tiktok', 'twitter', 'facebook', 'news'])
        .filter(p => p !== 'twitter' || project.platforms?.includes('twitter'))   // Twitter optional
        .filter(p => p !== 'threads' || project.platforms?.includes('threads'));  // Threads optional

      if (keywords.length === 0) continue;

      // Push status update ke UI
      if (wsHub) {
        wsHub.broadcastAll({
          type: 'scheduler_progress',
          project: project.name,
          status: 'scraping',
          keywords: keywords.length,
          platforms: platforms.length,
        });
      }

      for (const keyword of keywords) {
        const maxResults = config.get('scheduler_max_results');
        // Scrape parallel semua platform untuk keyword ini
        const scrapeResults = await Promise.allSettled(
          platforms.map(async (platform) => {
            const data = await scrapeViaScraperPool(platform, keyword, maxResults);
            return { platform, data };
          })
        );

        // Persist hasil scrape ke MongoDB
        let totalInserted = 0;
        for (const r of scrapeResults) {
          if (r.status !== 'fulfilled') continue;
          const { platform, data } = r.value;
          const posts = data?.posts || [];
          for (const p of posts) {
            try {
              if (p.external_id) {
                const existing = await db.collection('posts').findOne({
                  project_id: String(project._id),
                  external_id: p.external_id,
                });
                if (existing) continue;
              }
              await db.collection('posts').insertOne({
                project_id: String(project._id),
                external_id: p.external_id || p.id,
                platform: p.platform || platform,
                keyword_matched: keyword,
                author: p.author || p.username || p.ownerUsername,
                handle: p.handle || p.username,
                avatar: p.avatar || p.profile_pic_url || p.profilePicUrl,
                content: p.content || p.text || p.caption,
                views: p.views || p.video_view_count || p.videoViewCount || 0,
                likes: p.likes || p.like_count || p.likesCount || 0,
                shares: p.shares || 0,
                comments: p.comments || p.comment_count || p.commentsCount || 0,
                sentiment: p.sentiment || classifySentimentBasic(p.content || p.text || p.caption || ''),
                cities: typeof p.cities === 'string' ? p.cities : JSON.stringify(p.cities || []),
                hashtags: typeof p.hashtags === 'string' ? p.hashtags : JSON.stringify(p.hashtags || []),
                source_name: p.source_name,
                url: p.url,
                post_date: p.post_date ? new Date(p.post_date) : (p.timestamp ? new Date(p.timestamp * 1000) : null),
                created_at: new Date(),
              });
              totalInserted++;
            } catch (e) {
              // ignore duplicates / individual errors
            }
          }
        }
        projectResult.keywords_scraped++;
        projectResult.platforms_scraped += platforms.length;

        // Run alert detectors untuk keyword ini
        try {
          const alerts = await runAllDetectors(keyword, String(project._id), ctx);
          // Tambahkan project name ke setiap alert untuk display
          for (const a of alerts) a.project_name = project.name;
          projectResult.alerts.push(...alerts);
          cycleResults.total_alerts += alerts.length;
        } catch (e) {
          console.error(`[scheduler] alerts failed for "${keyword}":`, e.message);
        }
      }

      cycleResults.projects.push(projectResult);
    }

    cycleResults.duration_ms = Date.now() - startedAt;
    cycleResults.finished_at = new Date();
    _lastResults = cycleResults;
    _lastRunAt = new Date();

    // Persist cycle history
    try {
      await db.collection('scheduler_history').insertOne(cycleResults);
    } catch (e) {
      console.warn('[scheduler] failed to save history:', e.message);
    }

    console.log(`[scheduler] cycle done in ${cycleResults.duration_ms}ms — ${cycleResults.total_alerts} alert(s) dispatched`);

    // Push final status ke UI
    if (wsHub) {
      wsHub.broadcastAll({
        type: 'scheduler_complete',
        cycle: {
          duration_ms: cycleResults.duration_ms,
          projects: cycleResults.projects.length,
          alerts: cycleResults.total_alerts,
        },
      });
    }
  } catch (e) {
    console.error('[scheduler] cycle error:', e);
  } finally {
    _running = false;
  }
}

// ─── Daily briefing — jam 7 pagi WIB ─────────────────────────────────
async function runDailyBriefing(ctx) {
  const { db } = ctx;
  try {
    // Aggregate: total mention 24 jam, perubahan vs 24-48 jam sebelumnya
    const oneDayAgo = new Date(Date.now() - 24 * 3600 * 1000);
    const twoDaysAgo = new Date(Date.now() - 48 * 3600 * 1000);

    const totalToday = await db.collection('posts').countDocuments({ created_at: { $gte: oneDayAgo } });
    const totalYesterday = await db.collection('posts').countDocuments({
      created_at: { $gte: twoDaysAgo, $lt: oneDayAgo },
    });
    const volumeChangePct = totalYesterday > 0
      ? ((totalToday - totalYesterday) / totalYesterday) * 100
      : 0;

    const sentimentAgg = await db.collection('posts').aggregate([
      { $match: { created_at: { $gte: oneDayAgo } } },
      { $group: { _id: '$sentiment', count: { $sum: 1 } } },
    ]).toArray();
    const sentimentMap = {};
    for (const s of sentimentAgg) sentimentMap[s._id || 'neutral'] = s.count;
    const sentimentSummary = `${sentimentMap.positive || 0} positif / ${sentimentMap.neutral || 0} netral / ${sentimentMap.negative || 0} negatif`;

    const alerts24h = await db.collection('alerts')
      .find({ detected_at: { $gte: oneDayAgo } })
      .sort({ detected_at: -1 })
      .limit(20)
      .toArray();

    const topViral = await db.collection('posts').aggregate([
      { $match: { created_at: { $gte: oneDayAgo } } },
      { $addFields: { engagement: { $add: [
        { $ifNull: ['$likes', 0] },
        { $ifNull: ['$comments', 0] },
        { $ifNull: ['$views', 0] },
      ]}}},
      { $sort: { engagement: -1 } },
      { $limit: 5 },
    ]).toArray();

    await sendDailyBriefing({
      total_mentions: totalToday,
      volume_change_pct: volumeChangePct,
      sentiment_summary: sentimentSummary,
      alerts_24h: alerts24h,
      top_viral: topViral,
    });
    console.log('[scheduler] daily briefing sent');
  } catch (e) {
    console.error('[scheduler] daily briefing failed:', e);
  }
}

// ─── Sentiment classifier sederhana sebagai fallback ─────────────────
// Untuk production, ganti dengan model NLP real (mis. via OpenRouter) atau
// fine-tuned IndoBERT. Sekarang basic keyword matching biar scheduler bisa
// jalan even saat scraper tidak return field sentiment.
const POSITIVE_WORDS = ['bagus', 'mantap', 'keren', 'recommended', 'love', 'suka', 'cinta', 'puas', 'top', 'hebat', 'oke', 'good', 'great', 'amazing'];
const NEGATIVE_WORDS = ['jelek', 'buruk', 'kecewa', 'mengecewakan', 'tipu', 'penipu', 'bohong', 'parah', 'rusak', 'cacat', 'racun', 'beracun', 'palsu', 'kw', 'boikot'];

function classifySentimentBasic(text) {
  if (!text) return 'neutral';
  const lc = text.toLowerCase();
  let score = 0;
  for (const w of POSITIVE_WORDS) if (lc.includes(w)) score++;
  for (const w of NEGATIVE_WORDS) if (lc.includes(w)) score--;
  if (score > 0) return 'positive';
  if (score < 0) return 'negative';
  return 'neutral';
}

// ─── Public: start / stop scheduler with hot-reload ─────────────────
async function start(ctx) {
  await config.loadAll(); // warm cache

  if (!config.get('scheduler_enabled')) {
    console.log('[scheduler] disabled via config — not starting');
    return;
  }
  if (_interval) {
    console.log('[scheduler] already running');
    return;
  }

  _ctxRef = ctx;
  _currentIntervalMin = config.get('scheduler_interval_minutes');

  console.log(`[scheduler] starting — interval ${_currentIntervalMin}min`);

  // First cycle setelah 30 detik (biar semua service ready dulu)
  setTimeout(() => runCycle(ctx), 30 * 1000);

  // Subsequent cycles
  _interval = setInterval(
    () => runCycle(ctx),
    _currentIntervalMin * 60 * 1000
  );

  // Daily briefing — cek setiap menit, fire kalau jam 7 pagi WIB
  let lastBriefingDate = null;
  _briefingInterval = setInterval(async () => {
    const now = new Date();
    // Convert ke WIB (UTC+7)
    const wibHour = (now.getUTCHours() + 7) % 24;
    const today = now.toISOString().slice(0, 10);
    if (wibHour === 7 && lastBriefingDate !== today) {
      lastBriefingDate = today;
      await runDailyBriefing(ctx);
    }
  }, 60 * 1000);

  // Watcher: tiap 30 detik cek apakah config interval / enabled berubah dari UI
  if (!_watcher) {
    _watcher = setInterval(async () => {
      config.invalidate();
      await config.loadAll();
      const enabled = config.get('scheduler_enabled');
      const newInterval = config.get('scheduler_interval_minutes');

      if (!enabled && _interval) {
        console.log('[scheduler] disabled via UI — stopping');
        stop();
        return;
      }
      if (enabled && !_interval) {
        console.log('[scheduler] enabled via UI — restarting');
        await start(_ctxRef);
        return;
      }
      if (enabled && newInterval !== _currentIntervalMin) {
        console.log(`[scheduler] interval changed ${_currentIntervalMin} → ${newInterval}min, reloading`);
        stop();
        await start(_ctxRef);
      }
    }, 30 * 1000);
  }
}

function stop() {
  if (_interval) clearInterval(_interval);
  if (_briefingInterval) clearInterval(_briefingInterval);
  // Watcher tidak di-stop — supaya bisa auto-restart kalau config dinyalakan lagi
  _interval = null;
  _briefingInterval = null;
}

function status() {
  return {
    enabled: config.get('scheduler_enabled'),
    running: !!_interval,
    cycle_in_progress: _running,
    last_run_at: _lastRunAt,
    interval_minutes: _currentIntervalMin || config.get('scheduler_interval_minutes'),
    max_results_per_platform: config.get('scheduler_max_results'),
    last_results: _lastResults,
  };
}

// Manual trigger (untuk endpoint /api/scheduler/run-now)
async function triggerNow(ctx) {
  if (_running) return { error: 'cycle_in_progress' };
  runCycle(ctx);  // fire and forget
  return { triggered: true, started_at: new Date() };
}

module.exports = {
  start,
  stop,
  status,
  triggerNow,
  runDailyBriefing,
};