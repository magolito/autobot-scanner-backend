"""
Auth UI — real per-user login/register, replacing the shared
DASHBOARD_PASSWORD from Stage 1. Shared between dashboard.py and
meme_dashboard.py rather than duplicated in each, since the flow is
identical: both just need "give me an authenticated User or stop
rendering," they don't need their own copy of the form/rate-limiting logic.

Session persistence note, stated plainly rather than glossed over:
`st.session_state` survives reruns and interactions within a browser
tab's connection, but not a hard page refresh or a new tab — Streamlit
doesn't have built-in persistent cookie-based sessions. This is a real
limitation for Stage 2, not fixed here: it doesn't regress anything
(the Stage 1 shared-password login had the exact same limitation), but
"stay logged in across a refresh" would need a proper session-token +
cookie mechanism, which is a deliberate scope line for a later stage,
not an oversight.

Rate limiting is also session-scoped, same honest limitation already
documented for the Stage 1 shared password: it stops casual repeated
guessing in one browser tab, not a determined attacker rotating sessions.
"""

from __future__ import annotations
import time
from typing import Optional

import streamlit as st

from .app_storage import AppStorage, User

LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 30
MIN_PASSWORD_LENGTH = 8


def _render_login_tab(storage: AppStorage):
    if "auth_attempts" not in st.session_state:
        st.session_state.auth_attempts = 0
    if "auth_lockout_until" not in st.session_state:
        st.session_state.auth_lockout_until = None

    now = time.monotonic()
    locked = st.session_state.auth_lockout_until is not None and now < st.session_state.auth_lockout_until
    if locked:
        remaining = int(st.session_state.auth_lockout_until - now) + 1
        st.error(f"Too many failed attempts. Try again in {remaining}s.")
        return

    with st.form("login_form"):
        email = st.text_input("Email", placeholder="you@example.com")
        password = st.text_input("Password", type="password", placeholder="Password")
        submitted = st.form_submit_button("Sign in", width='stretch')
        if submitted:
            if not email or not password:
                st.error("Enter both email and password.")
                return
            user = storage.authenticate(email, password)
            if user is not None:
                st.session_state.user = user
                st.session_state.auth_attempts = 0
                st.session_state.auth_lockout_until = None
                st.rerun()
            else:
                st.session_state.auth_attempts += 1
                if st.session_state.auth_attempts >= LOGIN_MAX_ATTEMPTS:
                    st.session_state.auth_lockout_until = time.monotonic() + LOGIN_LOCKOUT_SECONDS
                    st.session_state.auth_attempts = 0
                    st.error(f"Too many failed attempts. Locked for {LOGIN_LOCKOUT_SECONDS}s.")
                else:
                    st.error(f"Incorrect email or password. {LOGIN_MAX_ATTEMPTS - st.session_state.auth_attempts} attempt(s) remaining.")


def _render_register_tab(storage: AppStorage):
    with st.form("register_form"):
        email = st.text_input("Email", placeholder="you@example.com", key="register_email")
        password = st.text_input("Password", type="password", placeholder=f"At least {MIN_PASSWORD_LENGTH} characters", key="register_password")
        confirm = st.text_input("Confirm password", type="password", key="register_confirm")
        submitted = st.form_submit_button("Create account", width='stretch')
        if submitted:
            if not email or "@" not in email:
                st.error("Enter a valid email address.")
                return
            if len(password) < MIN_PASSWORD_LENGTH:
                st.error(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
                return
            if password != confirm:
                st.error("Passwords don't match.")
                return
            user = storage.create_user(email, password)
            if user is None:
                st.error("An account with that email already exists — try signing in instead.")
                return
            st.session_state.user = user
            st.rerun()


def require_auth(storage: AppStorage, product_name: str = "AutoBot Scanner") -> User:
    """
    Main entry point — call near the top of a dashboard script, right
    after settings/storage error handling. Returns the authenticated
    User if already logged in (no UI shown). Otherwise renders the
    login/register screen and calls st.stop() — this function never
    returns None; the caller can always trust it got a real User back,
    or the script already stopped.

    Re-fetches the user record from storage on every call (a single
    cheap primary-key lookup) rather than trusting the cached session
    object indefinitely — a Stripe/crypto webhook is handled by the
    separate API service, not this dashboard process, so without this
    refresh a plan upgrade wouldn't take effect until the person logged
    out and back in. Streamlit reruns the whole script on every
    interaction, so this refresh happens naturally on the next click,
    not just the next page load.
    """
    if "user" not in st.session_state:
        st.session_state.user = None
    if st.session_state.user is not None:
        fresh = storage.get_user_by_id(st.session_state.user.id)
        if fresh is None:
            # account was deleted mid-session — treat like a logout rather than crashing on stale data
            st.session_state.user = None
        else:
            st.session_state.user = fresh
            return fresh

    _, center, _ = st.columns([1, 1.2, 1])
    with center:
        st.markdown('<div class="login-wrap">', unsafe_allow_html=True)
        st.markdown(f'<div class="login-logo">{product_name}</div>', unsafe_allow_html=True)
        st.markdown('<div class="login-title">Welcome</div>', unsafe_allow_html=True)
        st.markdown('<div class="login-sub">Sign in or create an account</div>', unsafe_allow_html=True)

        tab_login, tab_register = st.tabs(["Sign In", "Create Account"])
        with tab_login:
            _render_login_tab(storage)
        with tab_register:
            _render_register_tab(storage)

        st.markdown('</div>', unsafe_allow_html=True)

    st.stop()
    # st.stop() halts execution in every real Streamlit run. If this line
    # is ever reached, something has gone wrong with that guarantee (e.g.
    # code executed outside a real script-run context) — fail loudly
    # rather than silently returning None and letting a caller proceed
    # as if a user were authenticated when none exists.
    raise RuntimeError("require_auth(): st.stop() did not halt execution — refusing to return an unauthenticated session as if it were valid.")


def render_logout_button(help_text: str = "Sign out"):
    """Call in the top bar. Clears the session and reruns."""
    if st.button("⏻", help=help_text):
        st.session_state.user = None
        st.rerun()
