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
to resolve a logged-in user via modules/auth.validate_session(). No `Secure`
flag is set: this project's dev/test workflow serves over both plain
http://localhost and an HTTPS cloudflared tunnel, and a Secure cookie set
from localhost behaves inconsistently across browsers. Documented Phase 1
limitation — revisit before any exposure beyond current manual dev/test use.
"""
import streamlit.components.v1 as components

COOKIE_NAME = "eg_session"


def set_session_cookie(token: str, ttl_seconds: int) -> None:
    snippet = f"""
    <script>
    window.parent.document.cookie =
      "{COOKIE_NAME}=" + {token!r} + "; path=/; max-age=" + {int(ttl_seconds)} + "; SameSite=Lax";
    </script>
    """
    components.html(snippet, height=0, width=0)


def clear_session_cookie() -> None:
    snippet = f"""
    <script>
    window.parent.document.cookie = "{COOKIE_NAME}=; path=/; max-age=0; SameSite=Lax";
    </script>
    """
    components.html(snippet, height=0, width=0)
