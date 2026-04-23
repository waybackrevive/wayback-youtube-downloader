/**
 * WaybackRevive — Cloudflare Worker Route
 *
 * This worker runs on your MAIN DOMAIN: waybackrevive.com/api/*
 * It proxies all /api/* requests to your Render.com backend.
 *
 * Setup in Cloudflare Dashboard:
 *   Workers & Pages → Create Worker → paste this code
 *   Then: Workers & Pages → your worker → Triggers → Add Route:
 *     Route:   waybackrevive.com/api/*
 *     Zone:    waybackrevive.com
 *
 * Set environment variable in the worker settings:
 *   BACKEND_URL = https://waybackrevive-api.onrender.com
 */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        status: 204,
        headers: {
          'Access-Control-Allow-Origin': 'https://waybackrevive.com',
          'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
          'Access-Control-Allow-Headers': 'Content-Type, x-api-key',
          'Access-Control-Max-Age': '86400',
        },
      });
    }

    const BACKEND = (env.BACKEND_URL || '').replace(/\/$/, '');

    if (!BACKEND) {
      return Response.json(
        { detail: 'BACKEND_URL is not configured in Worker environment variables.' },
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
          'x-forwarded-for':
            request.headers.get('CF-Connecting-IP') ||
            request.headers.get('x-forwarded-for') ||
            '',
        },
        body: ['GET', 'HEAD'].includes(request.method) ? null : request.body,
        redirect: 'follow',
      });

      const resp = await fetch(proxyReq);
      const headers = new Headers(resp.headers);
      headers.set('Access-Control-Allow-Origin', 'https://waybackrevive.com');
      headers.set('Cache-Control', 'no-cache, no-store');

      return new Response(resp.body, {
        status: resp.status,
        statusText: resp.statusText,
        headers,
      });
    } catch (err) {
      return Response.json(
        { detail: `Backend unreachable: ${err.message}` },
        {
          status: 503,
          headers: { 'Access-Control-Allow-Origin': 'https://waybackrevive.com' },
        }
      );
    }
  },
};
