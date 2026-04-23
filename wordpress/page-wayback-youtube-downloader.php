<?php
/**
 * Template Name: Wayback YT Downloader
 *
 * ─────────────────────────────────────────────────────────────
 * INSTALLATION  (5 minutes, no coding needed)
 * ─────────────────────────────────────────────────────────────
 *
 * STEP 1 — Copy TWO things to your active WordPress theme folder:
 *
 *   wp-content/themes/YOUR-THEME/
 *   ├── page-wayback-youtube-downloader.php     ← this file
 *   └── wayback-tool/
 *       └── yt-deleted-video-recovery.html      ← the tool HTML
 *
 *   (Use FTP, cPanel File Manager, or your hosting's file manager)
 *
 * STEP 2 — Create a WordPress page:
 *   Admin → Pages → Add New
 *   · Title:    Wayback YouTube Downloader  (any title is fine)
 *   · Slug:     wayback-youtube-downloader  ← controls the URL
 *   · Template: "Wayback YT Downloader"     ← in Page Attributes panel
 *   · Publish
 *
 * STEP 3 — Set up the Cloudflare Worker API route:
 *   The tool HTML calls /api/* on the same domain.
 *   You need a Cloudflare Worker routing waybackrevive.com/api/* → Render.
 *   See yt-tool/cloudflare/api-proxy-route-worker.js + DEPLOYMENT.md Step 3.
 *
 * RESULT: https://waybackrevive.com/wayback-youtube-downloader  ✅
 *
 * ─────────────────────────────────────────────────────────────
 * HOW THE ARCHITECTURE WORKS
 * ─────────────────────────────────────────────────────────────
 *
 *   Browser visits waybackrevive.com/wayback-youtube-downloader
 *       ↓
 *   WordPress serves this PHP template (bypasses WP theme completely)
 *       ↓
 *   Browser renders the full tool HTML
 *       ↓
 *   User pastes URL → JS calls POST /api/check (same domain)
 *       ↓
 *   Cloudflare Worker on waybackrevive.com/api/* intercepts
 *       ↓
 *   Worker proxies request to https://waybackrevive-api.onrender.com
 *       ↓
 *   Render (FastAPI + yt-dlp) processes and returns result
 *
 * No iframe. No subdomain. No CORS. Same domain = SEO-friendly.
 *
 */

// WordPress security check
defined('ABSPATH') || exit;

// Locate the HTML tool file inside the active theme folder
$tool_file = get_stylesheet_directory() . '/wayback-tool/yt-deleted-video-recovery.html';

if (!file_exists($tool_file)) {
    http_response_code(500);
    echo '<!DOCTYPE html><html><head><title>Setup Required</title>
    <style>body{font-family:sans-serif;max-width:600px;margin:80px auto;padding:20px;color:#1e293b}
    code{background:#f1f5f9;padding:2px 6px;border-radius:4px;font-size:13px}</style></head><body>
    <h2>⚙️ Tool Setup Required</h2>
    <p>The tool HTML file is missing. Upload it to:</p>
    <p><code>wp-content/themes/YOUR-THEME/wayback-tool/yt-deleted-video-recovery.html</code></p>
    <p>See <strong>yt-tool/DEPLOYMENT.md</strong> for full instructions.</p>
    </body></html>';
    exit;
}

// Clear any WP output buffering, serve our HTML directly
if (ob_get_level()) {
    ob_end_clean();
}

header('Content-Type: text/html; charset=UTF-8');
header('X-Robots-Tag: index, follow');
nocache_headers();

readfile($tool_file);
exit;
