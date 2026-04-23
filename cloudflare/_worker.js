/**
 * WaybackRevive — Cloudflare Pages Worker
 *
 * This file lives in the ROOT of your project (same folder as the HTML files).
 * Cloudflare Pages automatically picks it up as the edge worker.
 *
 * What it does:
 *   - All /api/* requests  → proxied to your Railway/Render backend
 *   - Everything else      → served as static files from Pages
 *
 * Setup:
 *   1. Deploy this folder to Cloudflare Pages (dashboard.cloudflare.com → Pages)
 *   2. Set environment variable: BACKEND_URL = https://your-backend.railway.app
 *   3. Add custom domain: tool.waybackrevive.com  (Pages → Custom domains)
 */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // ── CORS preflight ──
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        status: 204,
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
          'Access-Control-Allow-Headers': 'Content-Type, x-api-key',
          'Access-Control-Max-Age': '86400',
        },
      });
    }

    // ── Proxy /api/* to Python backend ──
    if (url.pathname.startsWith('/api/')) {
      const BACKEND = (env.BACKEND_URL || '').replace(/\/$/, '');
      if (!BACKEND) {
        return Response.json(
          { detail: 'BACKEND_URL environment variable is not set. See deployment instructions.' },
          { status: 503 }
        );
      }

      const targetUrl = BACKEND + url.pathname + url.search;

      try {
        const proxyReq = new Request(targetUrl, {
          method: request.method,
          headers: {
            'content-type': request.headers.get('content-type') || 'application/json',
            'x-api-key': request.headers.get('x-api-key') || '',
            // Pass real client IP to backend for rate limiting
            'x-forwarded-for': request.headers.get('CF-Connecting-IP') || request.headers.get('x-forwarded-for') || '',
          },
          body: ['GET', 'HEAD'].includes(request.method) ? null : request.body,
          redirect: 'follow',
        });

        const resp = await fetch(proxyReq);
        const respHeaders = new Headers(resp.headers);
        respHeaders.set('Access-Control-Allow-Origin', '*');
        respHeaders.set('Cache-Control', 'no-cache, no-store');

        return new Response(resp.body, {
          status: resp.status,
          statusText: resp.statusText,
          headers: respHeaders,
        });
      } catch (err) {
        return Response.json(
          { detail: `Backend unreachable: ${err.message}` },
          {
            status: 503,
            headers: { 'Access-Control-Allow-Origin': '*' },
          }
        );
      }
    }

    // ── All other paths → Cloudflare Pages static assets ──
    return env.ASSETS.fetch(request);
  },
};
