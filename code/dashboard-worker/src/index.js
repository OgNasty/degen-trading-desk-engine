// Serves the static desk from the ASSETS binding with correct types and no caching on data.
const TYPES = {
  ".json": "application/json; charset=utf-8",
  ".jsonl": "application/x-ndjson; charset=utf-8",
  ".md": "text/markdown; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".svg": "image/svg+xml",
  ".webp": "image/webp",
};
const NO_STORE = [".json", ".jsonl", ".md", ".html"];

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", { status: 405 });
    }
    let path = url.pathname;
    if (path === "/" || path === "") path = "/index.html";
    const assetUrl = new URL(path, url.origin);
    const res = await env.ASSETS.fetch(new Request(assetUrl.toString(), { method: request.method }));
    const headers = new Headers(res.headers);
    const ext = (path.match(/\.[a-z0-9]+$/i) || [""])[0].toLowerCase();
    if (TYPES[ext]) headers.set("content-type", TYPES[ext]);
    if (NO_STORE.includes(ext)) {
      headers.set("cache-control", "no-store, max-age=0");
      headers.delete("etag");
    } else if (res.ok) {
      headers.set("cache-control", "public, max-age=300");
    }
    headers.set("x-content-type-options", "nosniff");
    // sec1014: security headers
    headers.set("content-security-policy", [
      "default-src 'self'",
      "script-src 'self' 'unsafe-inline' https://static.cloudflareinsights.com",
      "style-src 'self' 'unsafe-inline'",
      "img-src 'self' data: blob: https:",
      "font-src 'self' data:",
      "connect-src 'self' https://api.coingecko.com https://api.geckoterminal.com https://api.mainnet-beta.solana.com https://rpc.ankr.com https://solana-rpc.publicnode.com https://cloudflareinsights.com",
      "object-src 'none'",
      "base-uri 'self'",
      "form-action 'none'",
      "frame-ancestors 'none'",
    ].join("; "));
    headers.set("strict-transport-security", "max-age=31536000");
    headers.set("x-frame-options", "DENY");
    headers.set("referrer-policy", "strict-origin-when-cross-origin");
    headers.set("permissions-policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()");
    return new Response(res.body, { status: res.status, headers });
  },
};
