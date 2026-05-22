// backend/notifier.js
// Multi-channel notification untuk Slaytics — Telegram, Discord, WebSocket banner.
// Dipanggil oleh alerts.js ketika anomali terdeteksi.
//
// Konfigurasi: dibaca dari config helper (DB → env fallback).
// Setting bisa diubah lewat UI Settings tanpa restart container.

const config = require('./config');

// ─── Severity colors (untuk Discord embed dan UI banner) ─────────────
const SEVERITY = {
  critical: { color: 0xef4444, emoji: '🚨', label: 'CRITICAL' },
  high:     { color: 0xf59e0b, emoji: '⚠️', label: 'HIGH' },
  medium:   { color: 0x3b82f6, emoji: 'ℹ️', label: 'MEDIUM' },
  low:      { color: 0x10b981, emoji: '✓',  label: 'LOW' },
};

// ─── Telegram ────────────────────────────────────────────────────────
async function sendTelegram(text, opts = {}) {
  const token = config.get('telegram_bot_token');
  const chatId = config.get('telegram_chat_id');
  if (!token || !chatId) return { skipped: 'not_configured' };
  try {
    const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chat_id: chatId,
        text,
        parse_mode: opts.parse_mode || 'HTML',
        disable_web_page_preview: opts.no_preview ?? false,
      }),
    });
    const data = await r.json();
    if (!data.ok) return { error: data.description };
    return { ok: true, message_id: data.result.message_id };
  } catch (e) {
    console.error('[notifier] Telegram failed:', e.message);
    return { error: e.message };
  }
}

// ─── Discord ─────────────────────────────────────────────────────────
async function sendDiscord(embed) {
  const webhook = config.get('discord_webhook_url');
  if (!webhook) return { skipped: 'not_configured' };
  try {
    const r = await fetch(webhook, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: 'Slaytics Sentinel',
        embeds: [embed],
      }),
    });
    if (!r.ok) return { error: `HTTP ${r.status}` };
    return { ok: true };
  } catch (e) {
    console.error('[notifier] Discord failed:', e.message);
    return { error: e.message };
  }
}

// ─── Format alert untuk Telegram ────────────────────────────────────
function formatTelegramAlert(alert) {
  const sev = SEVERITY[alert.severity] || SEVERITY.medium;
  const lines = [
    `${sev.emoji} <b>${sev.label} — ${alert.type}</b>`,
    `<b>Keyword:</b> <code>${escapeHtml(alert.keyword)}</code>`,
    alert.project_name ? `<b>Project:</b> ${escapeHtml(alert.project_name)}` : '',
    '',
    escapeHtml(alert.message),
  ].filter(Boolean);

  // Tambah sample posts kalau ada (untuk negative spike)
  if (alert.samples?.length) {
    lines.push('', '<b>Sample post:</b>');
    for (const s of alert.samples.slice(0, 3)) {
      const author = s.handle || s.author || '?';
      const content = (s.content || '').slice(0, 180).replace(/\n/g, ' ');
      lines.push(`• <i>@${escapeHtml(author)}</i> [${s.platform}]: ${escapeHtml(content)}`);
      if (s.url) lines.push(`  ${s.url}`);
    }
  }

  // Saran action
  if (alert.recommended_action) {
    lines.push('', `<b>Saran:</b> ${escapeHtml(alert.recommended_action)}`);
  }

  lines.push('', `<i>${new Date(alert.detected_at || Date.now()).toLocaleString('id-ID')}</i>`);
  return lines.join('\n');
}

function formatDiscordAlert(alert) {
  const sev = SEVERITY[alert.severity] || SEVERITY.medium;
  const fields = [
    { name: 'Keyword', value: '`' + alert.keyword + '`', inline: true },
    { name: 'Type', value: alert.type, inline: true },
    { name: 'Severity', value: sev.label, inline: true },
  ];
  if (alert.metrics) {
    fields.push({
      name: 'Metrics',
      value: '```' + JSON.stringify(alert.metrics, null, 2) + '```',
      inline: false,
    });
  }
  if (alert.samples?.length) {
    const txt = alert.samples.slice(0, 3).map(s => {
      const author = s.handle || s.author || '?';
      const content = (s.content || '').slice(0, 200).replace(/\n/g, ' ');
      return `**@${author}** [${s.platform}]: ${content}${s.url ? `\n${s.url}` : ''}`;
    }).join('\n\n');
    fields.push({ name: 'Sample Posts', value: txt.slice(0, 1024) });
  }
  return {
    title: `${sev.emoji} ${sev.label} — ${alert.type}`,
    description: alert.message,
    color: sev.color,
    fields,
    timestamp: new Date(alert.detected_at || Date.now()).toISOString(),
    footer: { text: 'Slaytics Sentinel' },
  };
}

// ─── Main: dispatch alert ke semua channel ──────────────────────────
async function dispatchAlert(alert, { wsHub, db } = {}) {
  // Persist ke DB (untuk audit + dedup query)
  if (db) {
    try {
      await db.collection('alerts').insertOne({
        ...alert,
        dispatched_at: new Date(),
      });
    } catch (e) {
      console.warn('[notifier] failed to persist alert:', e.message);
    }
  }

  // Push ke WebSocket (banner di-monitor)
  if (wsHub) {
    wsHub.broadcastAll({
      type: 'critical_alert',
      alert,
      timestamp: Date.now(),
    });
  }

  // External notifications berdasarkan severity
  const results = { telegram: null, discord: null };

  // Critical & high → push ke semua channel
  // Medium → cuma in-app + Discord (lower urgency, tidak ganggu HP)
  // Low → in-app only
  if (alert.severity === 'critical' || alert.severity === 'high') {
    results.telegram = await sendTelegram(formatTelegramAlert(alert));
    results.discord = await sendDiscord(formatDiscordAlert(alert));
  } else if (alert.severity === 'medium') {
    results.discord = await sendDiscord(formatDiscordAlert(alert));
  }

  console.log(`[notifier] ${alert.severity}/${alert.type} for "${alert.keyword}" — TG:${results.telegram?.ok ? 'ok' : results.telegram?.skipped || 'fail'} DC:${results.discord?.ok ? 'ok' : results.discord?.skipped || 'fail'}`);

  return results;
}

// ─── Daily briefing ─────────────────────────────────────────────────
// Dipanggil tiap pagi jam 7 (atau saat boot) untuk kasih Bos ringkasan harian
async function sendDailyBriefing(summary) {
  const lines = [
    `☀️ <b>SELAMAT PAGI, BOS</b>`,
    `<i>Ringkasan ${new Date().toLocaleDateString('id-ID', { weekday: 'long', day: 'numeric', month: 'long' })}</i>`,
    '',
    `📊 <b>Total mention 24 jam:</b> ${summary.total_mentions || 0}`,
    `📈 <b>Volume change:</b> ${summary.volume_change_pct >= 0 ? '+' : ''}${summary.volume_change_pct?.toFixed(0) || 0}% vs kemarin`,
    `😊 <b>Sentiment:</b> ${summary.sentiment_summary || '—'}`,
    '',
  ];
  if (summary.alerts_24h?.length) {
    lines.push(`⚠️ <b>${summary.alerts_24h.length} alert dalam 24 jam:</b>`);
    for (const a of summary.alerts_24h.slice(0, 5)) {
      lines.push(`  • [${a.severity.toUpperCase()}] ${a.type} — ${a.keyword}`);
    }
    lines.push('');
  }
  if (summary.top_viral?.length) {
    lines.push(`🔥 <b>Top viral:</b>`);
    for (const v of summary.top_viral.slice(0, 3)) {
      lines.push(`  • @${v.handle || v.author} (${v.platform}): ${(v.content || '').slice(0, 100)}`);
    }
    lines.push('');
  }
  lines.push('Sistem siap. Buka command center untuk detail.');
  return await sendTelegram(lines.join('\n'));
}

function escapeHtml(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

module.exports = {
  dispatchAlert,
  sendTelegram,
  sendDiscord,
  sendDailyBriefing,
  SEVERITY,
};
