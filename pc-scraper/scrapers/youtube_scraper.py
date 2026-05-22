"""
YouTube Scraper
================
Pakai yt-dlp untuk search video (paling robust, support cookies, di-maintain aktif).
Untuk comments, pakai youtube-comment-downloader (lebih ringan dari yt-dlp's
--write-comments yang reload semua metadata).

Methods:
  scrape_keyword(keyword, amount)  -> search videos
  scrape_comments(video_url, amount) -> comments dari satu video

Frontend Slaytics bisa request comments via parameter `mode='comments'` di
endpoint /scrape (lihat app.py).
"""

import logging
import time
from typing import Dict, List, Optional

from .base import BaseScraper, to_jsonable

logger = logging.getLogger(__name__)


class YouTubeScraper(BaseScraper):
    
    PLATFORM = "youtube"
    
    def scrape_keyword(self, keyword: str, amount: int = 30) -> List[Dict]:
        return self.scrape_videos(keyword, amount)
    
    def scrape_videos(self, keyword: str, amount: int = 30) -> List[Dict]:
        username = self.account_manager.pick_next_active()
        if not username:
            raise Exception("No active YouTube accounts available (add 'anonymous' to use without login)")
        
        self.last_used_account = username
        cookies_path = self.account_manager.get_cookies_path(username)
        
        try:
            from yt_dlp import YoutubeDL
        except ImportError:
            raise Exception("yt-dlp not installed. Run: pip install yt-dlp")
        
        # === Note tentang extract_flat ===
        # Sebelumnya pakai extract_flat=True untuk speed, TAPI yt-dlp di mode
        # flat TIDAK return field 'timestamp' atau 'upload_date' — semua video
        # jadi punya taken_at=0, frontend render tanggal salah (jadi 1970 atau
        # "today" tergantung handling).
        # 
        # Bukti: GitHub issue yt-dlp #9642 ("Upload Date of videos - not working
        # in --flat-playlist mode") — upload_date returns NA in flat mode.
        # 
        # Sekarang pakai extract_flat=False supaya dapat timestamp lengkap.
        # Trade-off: search yang sebelumnya 10-30 detik untuk 100 video sekarang
        # bisa 1-2 menit (yt-dlp fetch metadata penuh per video). Mitigasi:
        #   - 'lazy_playlist=True' → streaming, gak block sampai semua selesai
        #   - 'playlist_items=1:N' → hard cap supaya gak over-fetch
        #   - Backend timeout YouTube sudah 90s, cukup untuk 30-50 video.
        #     Kalau user butuh max_results > 50, akan dapat partial result.
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,         # NEED FULL EXTRACT untuk timestamp
            'skip_download': True,
            'lazy_playlist': True,         # streaming, jangan block sampai semua selesai
            'playlist_items': f'1:{amount}',  # hard cap supaya tidak over-fetch
            'cookiefile': str(cookies_path) if cookies_path.read_text().strip() and not cookies_path.read_text().startswith('# anonymous') else None,
            'default_search': f'ytsearch{amount}',
            'noplaylist': False,
            # Network timeouts — fail fast supaya gak nyangkut di video bermasalah
            'socket_timeout': 10,
            # Skip video bermasalah (private/geo-block/deleted/age-restricted)
            # tanpa abort seluruh batch. Tanpa ini, 1 video unavailable di antara
            # ratusan hasil search bikin seluruh scrape gagal dengan
            # "ERROR: [youtube] <id>: This video is not available", lalu loop
            # retry di app.py ulang 3x dan tetep ketemu video yg sama. Dengan
            # ignoreerrors=True, entry untuk video itu jadi None dan ke-skip
            # di loop normalize (line ~86: `if not e: continue`).
            'ignoreerrors': True,
        }
        # remove None
        ydl_opts = {k: v for k, v in ydl_opts.items() if v is not None}
        
        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f'ytsearch{amount}:{keyword}', download=False)
            
            entries = info.get('entries') or []
            self.account_manager.mark_used(username)
            
            videos = []
            for e in entries:
                if not e:
                    continue
                try:
                    videos.append(self._normalize_video(e))
                except Exception as ex:
                    logger.warning(f"[youtube] normalize failed: {ex}")
            
            logger.info(f"[youtube] Scraped {len(videos)} videos for '{keyword}' via {username}")
            return videos
        except Exception as e:
            self.account_manager.mark_error(username, e)
            raise
    
    def scrape_comments(self, video_url_or_id: str, amount: int = 50) -> List[Dict]:
        """
        Comments dari satu video. Pakai youtube-comment-downloader (no auth needed
        biasanya, tapi cookies tetap dipakai kalau ada).
        """
        try:
            from youtube_comment_downloader import YoutubeCommentDownloader, SORT_BY_POPULAR
        except ImportError:
            raise Exception("Run: pip install youtube-comment-downloader")
        
        # Normalize ID -> URL
        if not video_url_or_id.startswith('http'):
            video_url_or_id = f'https://www.youtube.com/watch?v={video_url_or_id}'
        
        username = self.account_manager.pick_next_active()
        if username:
            self.last_used_account = username
            self.account_manager.mark_used(username)
        
        try:
            downloader = YoutubeCommentDownloader()
            gen = downloader.get_comments_from_url(video_url_or_id, sort_by=SORT_BY_POPULAR)
            
            comments = []
            for c in gen:
                if len(comments) >= amount:
                    break
                comments.append(self._normalize_comment(c, video_url_or_id))
            
            logger.info(f"[youtube] Scraped {len(comments)} comments for {video_url_or_id}")
            return comments
        except Exception as e:
            if username:
                self.account_manager.mark_error(username, e)
            raise
    
    @staticmethod
    def _normalize_video(e: dict) -> dict:
        vid = e.get('id') or e.get('url')
        
        # Timestamp extraction — multi-fallback chain.
        # yt-dlp full extract returns ts di field-field berbeda tergantung
        # extractor & video type:
        #   - 'timestamp': Unix timestamp (most reliable, prefered)
        #   - 'release_timestamp': untuk premiere/scheduled, reflects publish
        #   - 'upload_date': string 'YYYYMMDD' (always present di full extract)
        #   - 'release_date': string 'YYYYMMDD' fallback
        # Kita reject value 0 — itu indicator yt-dlp gagal extract, bukan
        # tanggal valid. Lebih baik biarkan None supaya frontend bisa display
        # "tanggal tidak diketahui" ketimbang misleading 1970-01-01 / hari ini.
        ts = None
        for field in ('timestamp', 'release_timestamp'):
            v = e.get(field)
            if v and isinstance(v, (int, float)) and v > 0:
                ts = int(v)
                break
        
        # Fallback ke string date YYYYMMDD (paling reliable di full extract)
        if ts is None:
            for field in ('upload_date', 'release_date'):
                ds = e.get(field)
                if ds and isinstance(ds, str) and len(ds) == 8 and ds.isdigit():
                    try:
                        from datetime import datetime, timezone
                        dt = datetime.strptime(ds, '%Y%m%d').replace(tzinfo=timezone.utc)
                        ts = int(dt.timestamp())
                        break
                    except Exception:
                        continue
        
        # Last resort — kalau yt-dlp gak return apa-apa, set None.
        # Frontend harus handle None gracefully (jangan convert jadi 0).
        # Kalau frontend lama yg expect angka, fallback minimum 0 — tapi
        # kita tandai dengan log warning supaya bisa di-trace.
        if ts is None:
            logger.warning(
                f"[youtube] No timestamp found for video {vid!r} "
                f"(fields tried: timestamp, release_timestamp, upload_date, release_date). "
                f"Available fields: {list(e.keys())[:20]}"
            )
            ts = 0  # fallback supaya frontend lama gak crash, tapi badge akan jelek
        
        # URL extraction — di full-extract mode, yt-dlp punya 2 field URL:
        #   - 'webpage_url': halaman YouTube (https://youtube.com/watch?v=...)  ← yang kita mau
        #   - 'url': streaming binary URL (https://rr1---xxx.googlevideo.com/videoplayback?...)
        #     → ini cuma valid sebentar (signed URL), gak bisa di-share
        #
        # Di flat mode (lama), 'url' isinya halaman YouTube — itu kenapa kode lama
        # naive `e.get('url')` jalan. Di full-extract (yang sekarang), 'url' =
        # streaming URL → kalau dipakai, tombol "Open Link" di frontend buka
        # binary mp4 stream yang expired beberapa menit kemudian.
        #
        # Order priority: webpage_url → fallback ke url HANYA kalau bukan
        # googlevideo.com domain → fallback ke construct dari video ID.
        page_url = e.get('webpage_url')
        if not page_url:
            raw_url = e.get('url') or ''
            # Filter: jangan pakai streaming URL sebagai page link
            if raw_url and 'googlevideo.com' not in raw_url and 'videoplayback' not in raw_url:
                page_url = raw_url
        if not page_url and vid:
            page_url = f'https://www.youtube.com/watch?v={vid}'
        
        return to_jsonable({
            'platform': 'youtube',
            'id': vid,
            'shortCode': vid,
            'ownerUsername': e.get('uploader') or e.get('channel'),
            'username': e.get('uploader') or e.get('channel'),
            'profilePicUrl': None,
            'profile_pic_url': None,
            'caption': e.get('title') or '',
            'text': (e.get('title') or '') + (('\n' + e.get('description', '')) if e.get('description') else ''),
            'title': e.get('title') or '',
            'description': e.get('description') or '',
            'likesCount': int(e.get('like_count') or 0),
            'like_count': int(e.get('like_count') or 0),
            'commentsCount': int(e.get('comment_count') or 0),
            'comment_count': int(e.get('comment_count') or 0),
            'videoViewCount': int(e.get('view_count') or 0),
            'video_view_count': int(e.get('view_count') or 0),
            'timestamp': ts,
            'taken_at': ts,
            'duration': int(e.get('duration') or 0),
            'url': page_url or '',
            'channel_id': e.get('channel_id'),
            'channel_url': e.get('channel_url'),
        })
    
    @staticmethod
    def _normalize_comment(c: dict, video_url: str) -> dict:
        ts = c.get('time_parsed') or c.get('time') or 0
        try:
            ts = int(ts) if isinstance(ts, (int, float)) else int(time.time())
        except Exception:
            ts = int(time.time())
        
        return to_jsonable({
            'platform': 'youtube',
            'type': 'comment',
            'id': c.get('cid') or c.get('id'),
            'parent_video_url': video_url,
            'ownerUsername': c.get('author'),
            'username': c.get('author'),
            'profilePicUrl': c.get('photo'),
            'profile_pic_url': c.get('photo'),
            'caption': c.get('text') or '',
            'text': c.get('text') or '',
            'likesCount': int(c.get('votes') or 0),
            'like_count': int(c.get('votes') or 0),
            'commentsCount': int(c.get('reply_count') or c.get('replies') or 0),
            'comment_count': int(c.get('reply_count') or c.get('replies') or 0),
            'videoViewCount': 0,
            'video_view_count': 0,
            'timestamp': ts,
            'taken_at': ts,
            'url': f"{video_url}&lc={c.get('cid')}" if c.get('cid') else video_url,
        })