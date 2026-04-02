# Slaytics v2 — Social Media Monitoring Platform

A comprehensive social media monitoring and analytics platform with multi-platform scraping, sentiment analysis, and AI-powered insights.

## Features

### 🔐 Authentication System
- User registration and login with JWT tokens
- Secure password hashing with bcrypt
- Session management with localStorage

### 📊 Dashboard & Analytics
- Real-time mentions tracking across platforms
- Sentiment analysis (positive/neutral/negative)
- Engagement metrics (views, likes, shares, comments)
- Platform distribution charts
- Trending hashtags word cloud

### 🌐 Multi-Platform Scraping
- **TikTok** — via Apify actor (with tikwm fallback)
- **Twitter/X** — via Apify actor
- **Instagram** — via Apify hashtag scraper
- **Facebook** — via Apify posts scraper
- **YouTube** — via Apify scraper
- **News** — via Google News scraper

### 📈 Analysis Views
1. **Dashboard** — Overview with charts, sentiment, platforms, trending
2. **Mentions** — All posts feed with keyword highlighting and dates
3. **Analysis** — Trend, hourly, sentiment charts, treemap, media
4. **SNA** — Social Network Analysis with interactive D3.js graph
5. **Locations** — City-based mention mapping (80+ Indonesian cities)
6. **Timeline** — Chronological post view
7. **Influencers** — Top authors ranked by reach
8. **Media Share** — Source distribution and news feed
9. **AI Analysis** — AI-powered SWOT, recommendations, executive summary
10. **Comparison** — Side-by-side project comparison

### 🤖 AI-Powered Analysis
- Executive Summary generation
- SWOT Analysis
- Strategic Recommendations
- Hashtag Cluster Analysis
- Powered by OpenRouter API (Gemini)

### 📄 Export Options
- CSV export with all data
- PDF report (10+ pages with charts, AI analysis)

### 🔧 Project Management
- Create, edit, and delete projects
- Multiple keywords per project
- Platform selection per project
- Color coding for easy identification

## Setup

### Using Docker (Recommended)
```bash
# Clone or extract the project
cd slaytics

# Build and run
docker-compose up --build -d

# Access the app
open http://localhost:3000
```

### Manual Setup
```bash
# Backend
cd backend
npm install
npm start

# Frontend (serve with any HTTP server)
cd frontend
npx serve -s . -l 3000
```

## Configuration

### Environment Variables
```bash
# Backend
PORT=3001
DB_PATH=/app/data/slaytics.db
JWT_SECRET=your-secret-key-here
```

### API Tokens Required
1. **Apify API Token** — For social media scraping
2. **OpenRouter API Token** — For AI analysis (optional)

Set these in the Settings modal within the app.

## Tech Stack

### Backend
- Node.js + Express
- SQLite (better-sqlite3)
- JWT Authentication
- bcrypt for password hashing

### Frontend
- Vanilla JavaScript (no framework)
- Chart.js for visualizations
- D3.js for SNA graph
- jsPDF for PDF generation
- Font Awesome icons

## Database Schema

- `users` — User accounts
- `projects` — Monitoring projects with keywords
- `posts` — Scraped social media posts
- `scrape_sessions` — Scraping history
- `scrape_checkpoints` — Incremental scraping state
- `settings` — API tokens and preferences

## API Endpoints

### Authentication
- `POST /api/auth/register` — Create account
- `POST /api/auth/login` — Login
- `GET /api/auth/me` — Get current user
- `PUT /api/auth/password` — Change password

### Projects
- `GET /api/projects` — List projects
- `POST /api/projects` — Create project
- `PUT /api/projects/:id` — Update project
- `DELETE /api/projects/:id` — Delete project

### Posts
- `GET /api/posts` — Get posts with filters
- `POST /api/posts/bulk` — Bulk insert posts

### Stats
- `GET /api/stats/:project_id` — Get project statistics

## License

MIT License

## Credits

Built with ❤️ for social media monitoring and brand analytics.
