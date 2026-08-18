"""Injects PWA meta tags / manifest / service-worker registration into the
Streamlit page's <head>.

Streamlit renders `components.v1.html` content inside a sandboxed iframe, so
we reach into `window.parent.document` (same-origin — it's the same Streamlit
server) to patch tags onto the real page <head>. This is a well-known
community pattern since Streamlit doesn't expose <head> injection natively.

Static files (manifest.json, sw.js, icons/*) must live under ./static and
`server.enableStaticServing = true` must be set in .streamlit/config.toml —
they are then served at `<app-url>/app/static/<file>`.
"""
import streamlit.components.v1 as components

_HEAD_SNIPPET = """
<script>
(function() {
  const doc = window.parent.document;
  const base = window.parent.location.origin;

  function ensureTag(selector, create) {
    if (doc.querySelector(selector)) return;
    const el = create();
    doc.head.appendChild(el);
  }

  // Web App Manifest
  ensureTag('link[rel="manifest"]', () => {
    const l = doc.createElement('link');
    l.rel = 'manifest';
    l.href = base + '/app/static/manifest.json';
    return l;
  });

  // Theme color (address bar / status bar tint)
  ensureTag('meta[name="theme-color"]', () => {
    const m = doc.createElement('meta');
    m.name = 'theme-color';
    m.content = '#111827';
    return m;
  });

  // iOS: enables standalone (full-screen, no Safari chrome) mode
  ensureTag('meta[name="apple-mobile-web-app-capable"]', () => {
    const m = doc.createElement('meta');
    m.name = 'apple-mobile-web-app-capable';
    m.content = 'yes';
    return m;
  });

  ensureTag('meta[name="apple-mobile-web-app-status-bar-style"]', () => {
    const m = doc.createElement('meta');
    m.name = 'apple-mobile-web-app-status-bar-style';
    m.content = 'black-translucent';
    return m;
  });

  ensureTag('meta[name="apple-mobile-web-app-title"]', () => {
    const m = doc.createElement('meta');
    m.name = 'apple-mobile-web-app-title';
    m.content = 'ElectroGrader';
    return m;
  });

  // iOS Home Screen icon
  ensureTag('link[rel="apple-touch-icon"]', () => {
    const l = doc.createElement('link');
    l.rel = 'apple-touch-icon';
    l.href = base + '/app/static/icons/icon-192.png';
    return l;
  });

  // Viewport tweaks for edge-to-edge full-screen on notched iPhones
  const viewport = doc.querySelector('meta[name="viewport"]');
  if (viewport) {
    viewport.content = 'width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover, user-scalable=no';
  }

  // Register the service worker against the PARENT window/origin (not this
  // sandboxed iframe) — best-effort; requires HTTPS or localhost. Scope is
  // capped by the browser to the directory the script itself is served
  // from (sw.js lives under /app/static/, via Streamlit's static file
  // serving — there's no way to serve it from the site root), so
  // requesting scope '/' is silently rejected and registration never
  // succeeds. '/app/static/' is the real max: still enough to control the
  // shell assets sw.js actually caches (manifest.json, icons/*), which is
  // all this service worker does — see static/sw.js.
  try {
    if (window.parent.navigator.serviceWorker) {
      window.parent.navigator.serviceWorker.register(base + '/app/static/sw.js', { scope: base + '/app/static/' }).catch(() => {});
    }
  } catch (e) { /* cross-origin or unsupported, ignore */ }
})();
</script>
"""


def inject_pwa_head() -> None:
    """Call once near the top of the Streamlit app (every rerun is fine —
    the JS is idempotent thanks to the ensureTag guards)."""
    components.html(_HEAD_SNIPPET, height=0, width=0)
