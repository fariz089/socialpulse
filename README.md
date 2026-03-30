# SocialPulse Pro v12 — Brand Monitoring Platform

## What's New in v12

### 1. NEW: Social Network Analysis (SNA) View
- **Interactive D3.js force-directed graph** with draggable nodes
- Nodes represent: People, Issues/Hashtags, Locations (color-coded)
- **Click any node** to see related posts in drilldown panel
- Side panels: Top People, Top Issues, Top Sources, Top Locations
- All items clickable → shows related posts

### 2. NEW: Locations View
- Grid of detected cities with mention counts
- **Click any city** → drilldown shows all posts from that location
- Bar chart showing location distribution
- Locations detected from post content text (34 Indonesian cities)

### 3. NEW: Timeline View
- Posts grouped by date, sorted by views
- Click any post to open original URL
- Platform icons and view counts visible

### 4. NEW: Media Share View
- Full source distribution chart (doughnut)
- All sources listed with click-to-drilldown
- Latest news feed section

### 5. NEW: Drilldown Panel
- Slide-in panel from right side
- Shows filtered posts when you click ANY item:
  - Location → posts from that city
  - Person → posts by that author
  - Hashtag → posts containing that hashtag
  - Source → posts from that media source
  - Platform → posts from that platform

### 6. All Dashboard Items Now Clickable
- Trending hashtags → click to see related posts
- Platform bars → click to filter by platform
- Treemap items → click to see posts with that hashtag
- Media sources → click to see posts from that source
- Influencers → click to see their posts

### 7. Full 12-Page PDF Report (unchanged from v11)
- Cover, Executive Summary, Volume & Trends
- Sentiment Analysis, Hot Issues, Timeline
- SNA, Media Share, Influencers, Top Mentions
- Analisa & Saran, Thank You

## Menu Structure (9 views)
1. **Dashboard** — Overview with charts, sentiment, platforms, trending
2. **Mentions** — All posts feed with keyword highlighting
3. **Analysis** — Trend, hourly, sentiment charts, treemap, media
4. **SNA** — Social Network Analysis with interactive graph
5. **Locations** — City-based mention mapping
6. **Timeline** — Chronological post view
7. **Influencers** — Top authors ranked by reach
8. **Media Share** — Source distribution and news feed
9. **Comparison** — Side-by-side project comparison

## Setup
```bash
tar -xzf socialpulse-pro-v12-complete.tar.gz
docker-compose up --build -d
```
Open http://localhost:3000

## Data Location Logic
Locations are detected by scanning post content for Indonesian city names 
(Jakarta, Surabaya, Malang, Bali, etc — 34 cities). This is text matching, 
not GPS geolocation. When a post mentions "Malang" in its caption, it gets 
tagged with that city.
