"""
Standalone web app for backup-monitor: login (username + password + TOTP),
session-based auth, user management, and a read-only reports dashboard.

This is deliberately separate from the NS8 admin UI: it is meant to be
reached directly at the module's own FQDN (via the Traefik route set up
by configure-module), for people who need to check backup status without
having NS8 cluster-admin access. IMAP configuration is NOT exposed here;
it stays in the NS8 admin panel's Settings page.

User accounts (including who can manage other users) are entirely
self-contained in this app: the very first visit, with no users in the
database yet, is redirected to a one-time /setup flow that creates the
first admin account. From then on, admins manage further accounts from
/admin/users - none of this goes through NS8 actions.
"""

from __future__ import annotations

import os
import secrets
import time
from functools import wraps

from flask import Flask, g, redirect, render_template, request, session, url_for, jsonify
from flask_wtf import CSRFProtect

import authlib
import poller
from db import get_db

SECRET_KEY_PATH = os.environ.get("FLASK_SECRET_KEY_PATH", "/state/flask_secret_key")
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60


def _load_or_create_secret_key() -> bytes:
    if os.path.exists(SECRET_KEY_PATH):
        with open(SECRET_KEY_PATH, "rb") as f:
            key = f.read().strip()
            if key:
                return key
    key = secrets.token_bytes(32)
    with open(SECRET_KEY_PATH, "wb") as f:
        f.write(key)
    os.chmod(SECRET_KEY_PATH, 0o600)
    return key


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = _load_or_create_secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # The module is only ever reached over HTTPS via Traefik in
        # production; allow disabling the "Secure" cookie flag for local
        # testing over plain HTTP.
        SESSION_COOKIE_SECURE=os.environ.get("INSECURE_HTTP") != "true",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 8,  # 8 hours
    )

    csrf = CSRFProtect(app)

    @app.after_request
    def set_security_headers(response):
        # Prevents the page from being embedded in a frame on another
        # site (clickjacking); redundant with the CSP frame-ancestors
        # directive below on modern browsers, kept for older ones too.
        response.headers["X-Frame-Options"] = "DENY"
        # Stops browsers from MIME-sniffing a response away from its
        # declared Content-Type.
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Only ever reached over HTTPS via Traefik in production (see
        # SESSION_COOKIE_SECURE above) - tell browsers to enforce that
        # for a year, including subdomains.
        if os.environ.get("INSECURE_HTTP") != "true":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        # No external resources are loaded (Google Fonts was removed
        # specifically so nothing here needs a Subresource Integrity
        # exception): everything comes from this same origin, plain
        # inline <style>/<script> for the hand-written dashboard JS.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        return response

    def get_db_conn():
        if "db" not in g:
            g.db = get_db()
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("username"):
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)

        return wrapped

    def admin_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("username"):
                return redirect(url_for("login", next=request.path))
            if not session.get("is_admin"):
                return ("Forbidden: administrator access required", 403)
            return view(*args, **kwargs)

        return wrapped

    def any_users_exist(db) -> bool:
        return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    @app.before_request
    def require_setup_first():
        # If there isn't a single user yet, force everyone to the one-time
        # setup flow that creates the first admin account - there is no
        # other way to create users (no NS8 action, no open registration).
        if request.endpoint in ("setup", "setup_verify", "healthz", "static"):
            return None
        db = get_db_conn()
        if not any_users_exist(db) and request.endpoint != "setup":
            return redirect(url_for("setup"))
        return None

    # -- login attempt throttling -------------------------------------

    def is_locked_out(db, username: str) -> bool:
        row = db.execute(
            "SELECT locked_until FROM login_attempts WHERE username = ?", (username,)
        ).fetchone()
        return bool(row and row["locked_until"] > time.time())

    def register_failed_attempt(db, username: str) -> None:
        row = db.execute(
            "SELECT failed_count FROM login_attempts WHERE username = ?", (username,)
        ).fetchone()
        failed_count = (row["failed_count"] if row else 0) + 1
        locked_until = 0.0
        if failed_count >= MAX_FAILED_ATTEMPTS:
            locked_until = time.time() + LOCKOUT_SECONDS
            failed_count = 0
        db.execute(
            """
            INSERT INTO login_attempts (username, failed_count, locked_until)
            VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                failed_count = excluded.failed_count,
                locked_until = excluded.locked_until
            """,
            (username, failed_count, locked_until),
        )
        db.commit()

    def clear_failed_attempts(db, username: str) -> None:
        db.execute("DELETE FROM login_attempts WHERE username = ?", (username,))
        db.commit()

    # -- routes ----------------------------------------------------------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            totp_code = request.form.get("totp_code") or ""
            db = get_db_conn()

            if is_locked_out(db, username):
                error = "account_locked"
            else:
                user = db.execute(
                    "SELECT * FROM users WHERE username = ? AND disabled = 0", (username,)
                ).fetchone()
                valid = (
                    user is not None
                    and authlib.verify_password(password, user["password_hash"])
                    and authlib.verify_totp(user["totp_secret"], totp_code)
                )
                if valid:
                    clear_failed_attempts(db, username)
                    session.clear()
                    session.permanent = True
                    session["username"] = username
                    session["is_admin"] = bool(user["is_admin"])
                    next_url = request.args.get("next") or url_for("dashboard")
                    return redirect(next_url)
                else:
                    register_failed_attempt(db, username)
                    error = "invalid_credentials"

        return render_template("login.html", error=error)

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def dashboard():
        return render_template(
            "dashboard.html", username=session["username"], is_admin=session.get("is_admin", False)
        )

    @app.route("/api/reports")
    @login_required
    def api_reports():
        db = get_db_conn()

        severities = request.args.getlist("severity")
        destination = request.args.get("destination") or ""
        sender = request.args.get("sender") or ""
        search = request.args.get("search") or ""

        query = "SELECT * FROM reports WHERE 1=1"
        params: list = []

        if severities:
            placeholders = ",".join("?" for _ in severities)
            query += f" AND severity IN ({placeholders})"
            params.extend(severities)
        if destination:
            query += " AND destination = ?"
            params.append(destination)
        if sender:
            query += " AND sender = ?"
            params.append(sender)
        if search:
            query += " AND (user LIKE ? OR backup_set LIKE ?)"
            like = f"%{search}%"
            params.extend([like, like])

        # LIMIT is generous on purpose: during a large historical
        # reprocessing run (e.g. right after adding a new folder or
        # sender), thousands of never-before-seen messages get inserted
        # in a burst with a fresh received_at each - a small limit would
        # make the dashboard appear to "lose" reports from a folder that
        # just finished as soon as the next folder's messages start
        # arriving, rather than showing everything accumulated so far.
        query += " ORDER BY received_at DESC LIMIT 5000"

        rows = [dict(r) for r in db.execute(query, params).fetchall()]

        # Summary and the destinations list are scoped to the selected sender
        # "panel" (if any), so switching sender tabs really behaves like
        # switching to a dedicated view for that sender - but not to the
        # severity/destination/search filters, so those counters stay a
        # useful "how many would show up if I cleared severity/search"
        scope_query_suffix = ""
        scope_params: list = []
        if sender:
            scope_query_suffix = " AND sender = ?"
            scope_params = [sender]

        summary = {"CRITICAL": 0, "WARNING": 0, "OK": 0, "INFO": 0}
        for row in db.execute(
            f"SELECT severity, COUNT(*) AS n FROM reports WHERE 1=1{scope_query_suffix} GROUP BY severity",
            scope_params,
        ):
            summary[row["severity"]] = row["n"]

        destinations = [
            r["destination"] for r in
            db.execute(
                f"SELECT DISTINCT destination FROM reports WHERE destination != ''{scope_query_suffix} ORDER BY destination",
                scope_params,
            )
        ]

        # The senders list itself is always unscoped, so every tab stays
        # visible regardless of which one is currently selected.
        senders = [
            r["sender"] for r in
            db.execute("SELECT DISTINCT sender FROM reports WHERE sender IS NOT NULL AND sender != '' ORDER BY sender")
        ]

        return jsonify({
            "reports": rows, "summary": summary,
            "destinations": destinations, "senders": senders,
        })

    @app.route("/api/scan-now", methods=["POST"])
    @login_required
    def api_scan_now():
        poller.trigger_scan_now()
        return jsonify({"status": "ok"})

    @app.route("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    # -- first-run setup: create the first admin account -----------------

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        db = get_db_conn()
        if any_users_exist(db):
            return redirect(url_for("login"))

        error = None
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            if not username:
                error = "username_required"
            elif len(password) < 8:
                error = "password_too_short"
            else:
                secret = authlib.generate_totp_secret()
                db.execute(
                    "INSERT INTO users (username, password_hash, totp_secret, is_admin) VALUES (?,?,?,1)",
                    (username, authlib.hash_password(password), secret),
                )
                db.commit()
                session.clear()
                session["pending_username"] = username
                return redirect(url_for("setup_verify"))

        return render_template("setup.html", error=error)

    @app.route("/setup/verify", methods=["GET", "POST"])
    def setup_verify():
        username = session.get("pending_username")
        if not username:
            return redirect(url_for("setup"))
        db = get_db_conn()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not user:
            session.pop("pending_username", None)
            return redirect(url_for("setup"))

        error = None
        if request.method == "POST":
            code = request.form.get("totp_code") or ""
            if authlib.verify_totp(user["totp_secret"], code):
                session.clear()
                session.permanent = True
                session["username"] = username
                session["is_admin"] = True
                return redirect(url_for("dashboard"))
            error = "invalid_code"

        return render_template(
            "setup_verify.html",
            username=username,
            totp_secret=user["totp_secret"],
            totp_uri=authlib.provisioning_uri(user["totp_secret"], username),
            error=error,
        )

    # -- user management (admin only) -------------------------------------

    @app.route("/admin/users", methods=["GET"])
    @admin_required
    def admin_users():
        db = get_db_conn()
        users = db.execute(
            "SELECT username, is_admin, disabled, created_at FROM users ORDER BY username"
        ).fetchall()
        new_user_totp = session.pop("new_user_totp", None)
        return render_template(
            "admin_users.html",
            users=users,
            current_username=session["username"],
            new_user_totp=new_user_totp,
            error=session.pop("admin_users_error", None),
        )

    @app.route("/admin/users/create", methods=["POST"])
    @admin_required
    def admin_users_create():
        db = get_db_conn()
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        is_admin = 1 if request.form.get("is_admin") == "on" else 0

        if not username or len(password) < 8:
            session["admin_users_error"] = "invalid_input"
        elif db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            session["admin_users_error"] = "username_taken"
        else:
            secret = authlib.generate_totp_secret()
            db.execute(
                "INSERT INTO users (username, password_hash, totp_secret, is_admin) VALUES (?,?,?,?)",
                (username, authlib.hash_password(password), secret, is_admin),
            )
            db.commit()
            session["new_user_totp"] = {
                "username": username,
                "totp_secret": secret,
                "totp_uri": authlib.provisioning_uri(secret, username),
            }
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<username>/delete", methods=["POST"])
    @admin_required
    def admin_users_delete(username):
        db = get_db_conn()
        if username == session["username"]:
            session["admin_users_error"] = "cannot_delete_self"
            return redirect(url_for("admin_users"))

        remaining_admins = db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1 AND username != ?", (username,)
        ).fetchone()["n"]
        target = db.execute("SELECT is_admin FROM users WHERE username = ?", (username,)).fetchone()
        if target and target["is_admin"] and remaining_admins == 0:
            session["admin_users_error"] = "last_admin"
            return redirect(url_for("admin_users"))

        db.execute("DELETE FROM users WHERE username = ?", (username,))
        db.execute("DELETE FROM login_attempts WHERE username = ?", (username,))
        db.commit()
        return redirect(url_for("admin_users"))

    return app


app = create_app()
