"""Sets/clears the browser cookie that lets a login survive a hard refresh
or a brand-new tab — the one place besides app.py that's allowed to know
about both Streamlit and session tokens (modules/auth.py itself stays free
of any Streamlit import).

Uses the exact same `window.parent.document` idiom already proven live in
modules/pwa.py: components.v1.html renders inside a sandboxed iframe, but
it's same-origin (same Streamlit server), so reaching into the parent
document's real `document.cookie` works. st.context.cookies (read side) is
fixed at WebSocket-handshake time, so a cookie set here only becomes visible
to Python on the *next* fresh connection — that's the correct property for
this design: within a live connection, st.session_state already covers
"is this user still logged in".

COOKIE_NAME's value is a bearer session token — the only credential needed
to resolve a logged-in user via modules/auth.validate_session().

Environment-dependent cookie attributes (ELECTROGRADER_ENV, default
"development"):
  - development: no `Secure` flag — this project's dev/test workflow serves
    over both plain http://localhost and an HTTPS cloudflared tunnel, and a
    Secure cookie set from a plain-http localhost origin behaves
    inconsistently across browsers.
  - production (ELECTROGRADER_ENV=production): `Secure` is set, so the
    cookie is only ever sent over HTTPS.
  - `SameSite=Lax` in both modes, deliberately not changed between them —
    the task that introduced this config asked for "Lax, or Strict if it
    doesn't cause problems," and since nothing about this app's flow (no
    external redirects into a logged-in page) actually needs Strict,
    changing it would be a functional change for no real gain; Lax already
    blocks the cross-site cases that matter.
  - `HttpOnly` is NOT set, in either mode, and can't be from here: it's a
    protection against a cookie being *read* by JavaScript, so it can only
    be applied via a real HTTP `Set-Cookie` response header — never via
    `document.cookie`, which is exactly the mechanism this module has to
    use (Streamlit's standard `streamlit run` serving model doesn't expose
    a way to set arbitrary response headers per request). Achieving true
    HttpOnly would mean moving session-cookie issuance to something in
    front of Streamlit (a reverse proxy, or a custom ASGI wrapper) that can
    set that header itself — a real architecture change, out of scope
    here. Flagged rather than faked.
"""
import os

import streamlit.components.v1 as components

COOKIE_NAME = "eg_session"

ENVIRONMENT = os.environ.get("ELECTROGRADER_ENV", "development").strip().lower()
IS_PRODUCTION = ENVIRONMENT == "production"


def _cookie_attrs(ttl_seconds: int) -> str:
    attrs = f"path=/; max-age={int(ttl_seconds)}; SameSite=Lax"
    if IS_PRODUCTION:
        attrs += "; Secure"
    return attrs


def set_session_cookie(token: str, ttl_seconds: int) -> None:
    snippet = f"""
    <script>
    window.parent.document.cookie =
      "{COOKIE_NAME}=" + {token!r} + "; {_cookie_attrs(ttl_seconds)}";
    </script>
    """
    components.html(snippet, height=0, width=0)


def clear_session_cookie() -> None:
    snippet = f"""
    <script>
    window.parent.document.cookie = "{COOKIE_NAME}=; {_cookie_attrs(0)}";
    </script>
    """
    components.html(snippet, height=0, width=0)
