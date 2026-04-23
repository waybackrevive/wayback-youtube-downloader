# WaybackRevive — YT Deleted Video Recovery Tool

## Project Structure

```
yt-tool/
├── yt-deleted-video-recovery.html   ← Full standalone tool page (HTML+CSS+JS)
├── DEPLOYMENT.md                    ← This file
│
├── backend/                         ← Python FastAPI backend (deploy to Render)
│   ├── app.py                       · FastAPI app with yt-dlp + CDX API
│   ├── requirements.txt             · Python deps
│   ├── render.yaml                  · Render.com deploy config
│   ├── build.sh                     · Installs ffmpeg + pip deps
│   └── Procfile                     · Process definition
│
├── cloudflare/                      ← Cloudflare Worker files
│   ├── api-proxy-route-worker.js    · MAIN: routes waybackrevive.com/api/* → Render
│   ├── _worker.js                   · ALT: for standalone Cloudflare Pages deploy
│   └── wrangler.toml                · Cloudflare Pages config (ALT only)
│
└── wordpress/                       ← WordPress integration
    └── page-wayback-youtube-downloader.php  · WP page template
```

---

## Architecture (Recommended — WordPress on main domain)

```
User visits: waybackrevive.com/wayback-youtube-downloader
       ↓
WordPress serves page-wayback-youtube-downloader.php template
(bypasses WP theme — outputs our full HTML directly)
       ↓
Browser renders the tool. User pastes YouTube URL.
       ↓
JavaScript calls: POST /api/check  (same domain — relative URL)
       ↓
Cloudflare Worker on waybackrevive.com/api/* intercepts
(api-proxy-route-worker.js)
       ↓
Worker proxies to: https://waybackrevive-api.onrender.com/api/check
       ↓
Render.com (FastAPI + yt-dlp + ffmpeg) returns result
       ↓
Browser shows archive snapshots → user clicks Download
```

**Why this is the right architecture:**
- Same domain (`waybackrevive.com`) = no CORS, no iframe, no subdomain
- SEO-friendly — Google sees the full page content at `/wayback-youtube-downloader`
- WordPress handles the URL routing, Cloudflare handles the API proxy
- `BACKEND_URL = ''` in HTML — works everywhere automatically

---

## Step 1: Deploy Backend to Render.com (FREE)

### 1.1 — Push backend/ to GitHub

Create a new GitHub repo and push the `backend/` folder contents.

### 1.2 — Create Render Web Service

1. **dashboard.render.com** → New → Web Service
2. Connect GitHub repo
3. Settings:

| Setting | Value |
|---|---|
| Runtime | Python 3 |
| Build Command | `chmod +x build.sh && ./build.sh` |
| Start Command | `uvicorn app:app --host 0.0.0.0 --port $PORT` |
| Instance Type | **Free** |

4. Environment variables:

| Key | Value |
|---|---|
| `ALLOWED_ORIGINS` | `https://waybackrevive.com,https://www.waybackrevive.com` |
| `DOWNLOAD_DIR` | `/tmp/wayback_downloads` |
| `PRO_API_KEY` | `any-secret-key` (optional) |

5. Click **Create Web Service**

Your backend URL: `https://waybackrevive-api.onrender.com`

> **Note:** Free tier spins down after 15 min inactivity. First request takes ~60s. Subsequent requests are instant.

---

## Step 2: Set Up Cloudflare Worker on Main Domain

This is the key step — it routes `waybackrevive.com/api/*` to your Render backend.

### 2.1 — Create the Worker

1. **dash.cloudflare.com** → Workers & Pages → Create Application → Create Worker
2. Name it: `waybackrevive-api-proxy`
3. Click **Edit Code**, paste the contents of `cloudflare/api-proxy-route-worker.js`
4. Click **Save and Deploy**

### 2.2 — Set Environment Variable

In the Worker → Settings → Variables:
```
BACKEND_URL = https://waybackrevive-api.onrender.com
```

### 2.3 — Add Route Trigger

Worker → Triggers → Add Route:
```
Route:  waybackrevive.com/api/*
Zone:   waybackrevive.com
```

That's it. Now `waybackrevive.com/api/*` → Worker → Render backend.

---

## Step 3: WordPress Integration

This makes the tool available at `waybackrevive.com/wayback-youtube-downloader`.

### 3.1 — Upload files to your theme

Via FTP, cPanel, or SSH, upload these to your **active theme folder**:

```
wp-content/themes/YOUR-THEME/
├── page-wayback-youtube-downloader.php        ← from wordpress/
└── wayback-tool/
    └── yt-deleted-video-recovery.html         ← from yt-tool/ root
```

### 3.2 — Create the WordPress page

1. Admin → **Pages → Add New**
2. Fill in:
   - **Title:** Wayback YouTube Downloader  *(or any title)*
   - **Slug:** `wayback-youtube-downloader`  *(Page Attributes or URL settings)*
   - **Template:** "Wayback YT Downloader"  *(Page Attributes panel, right sidebar)*
3. Click **Publish**

### 3.3 — Verify

Visit: `https://waybackrevive.com/wayback-youtube-downloader`

You should see the full tool page — no WordPress header/footer, just the standalone tool.

### 3.4 — (Optional) Add to WordPress menu

Admin → Appearance → Menus → Add the page to your main navigation.

---

## Step 4: Test End-to-End

1. Open `https://waybackrevive.com/wayback-youtube-downloader`
2. Paste a live YouTube URL: `https://www.youtube.com/watch?v=dQw4w9WgXcQ`
3. Click "Find Video" — should show video info and archive snapshots
4. Click "Download Video" — progress bar should appear, then file downloads

---

## Troubleshooting

### Tool page shows "404" or WordPress default page
- Check the page slug is exactly `wayback-youtube-downloader`
- Check the page template is set to "Wayback YT Downloader"
- Make sure the PHP file was uploaded to the correct theme folder

### "Setup Required" error on the page
- The HTML file is missing from `wp-content/themes/YOUR-THEME/wayback-tool/`
- Upload `yt-deleted-video-recovery.html` into that folder

### "Backend unreachable" or API errors
- Check Cloudflare Worker logs: Workers → your worker → Logs
- Verify the Worker route is set to `waybackrevive.com/api/*`
- Verify `BACKEND_URL` env var in the Worker settings

### First request takes 60 seconds
- Normal for Render free tier (cold start). Subsequent requests are fast.
- The tool shows "Waking up backend..." message automatically.

### ffmpeg error on Render
- Check Render build logs — `build.sh` must complete without errors
- The `apt-get install ffmpeg` line must succeed

---

## Cost Summary

| Service | Cost |
|---|---|
| Render.com backend | $0 free tier |
| Cloudflare Worker | $0 free tier (100K requests/day) |
| WordPress hosting | Your existing hosting |
| **Total** | **$0/month** |
