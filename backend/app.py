"""
app.py - Flask application: routes (API), login/session handling, validation and orchestration.

Run:   python app.py      then open  http://127.0.0.1:5000
(Flask also serves ../frontend/index.html and script.js, so cookies/sessions work normally.)

Syllabus concepts shown here: decorators, dictionaries/JSON, threads (background e-mail),
exception handling, SQLite via database.py, and OOP classes from analyzer.py.
"""
import os
import json
import secrets
import sqlite3
import threading
import traceback
from datetime import datetime, timedelta
from functools import wraps

# Auto-load .env file if present
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(ENV_PATH):
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
    except Exception:
        pass

from flask import Flask, Response, g, jsonify, request, send_from_directory, session
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

import analyzer as an
import database as db
import utils

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(os.path.dirname(BASE_DIR), "frontend")

app = Flask(__name__)
# SECRET_KEY comes from the environment. If it is missing we create a random one for this run
# (fine for a local demo; users simply have to log in again after a restart).
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024            # reject uploads larger than 5 MB
CORS(app, supports_credentials=True,
     origins=[r"http://127\.0\.0\.1:\d+", r"http://localhost:\d+"])

db.init_db()                                                   # creates the SQLite tables on first run

# One Stack per logged-in user: keeps the latest device changes/imports (latest on top).
# It lives in memory, so it is cleared when the server restarts.
change_stacks = {}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def ok(message, data=None, status=200):
    return jsonify({"success": True, "message": message, "data": data}), status


def fail(message, status=400, **extra):
    body = {"success": False, "message": message}
    body.update(extra)
    return jsonify(body), status


def login_required(view):
    """DECORATOR: runs before the view; blocks the request unless a valid session exists."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        user_id = session.get("user_id")
        user = db.get_user_by_id(user_id) if user_id else None
        if user is None:
            session.clear()
            return fail("Please log in to continue.", 401)
        g.user = user
        return view(*args, **kwargs)
    return wrapper


def public_user(user):
    """User data that is safe to send to the browser (never the password hash)."""
    return {"id": user["id"], "name": user["name"], "email": user["email"],
            "consumer_type": user["consumer_type"], "billing_cycle": user["billing_cycle"],
            "tariff_mode": user["tariff_mode"], "custom_rate": user["custom_rate"],
            "profile_complete": bool(user["consumer_type"])}


def profile_required():
    if not g.user["consumer_type"]:
        return fail("Please complete the profile setup first.", 400)
    return None


def get_stack(user_id):
    return change_stacks.setdefault(user_id, an.Stack(max_size=20))


def tariff_summary(user):
    return {"mode": user["tariff_mode"], "custom_rate": user["custom_rate"],
            "default_slabs": an.DEFAULT_TARIFF,
            "note": "Default slabs are application defaults, not an official electricity-board tariff."}


# ---------------------------------------------------------------------------
# Orchestration: dashboard -> snapshot -> report -> e-mail
# ---------------------------------------------------------------------------
def build_dashboard(user_id):
    """Everything the website needs, calculated fresh from the database."""
    user = db.get_user_by_id(user_id)
    analysis = an.ElectricityAnalyzer(user, db.get_devices(user_id)).analyze()
    reports = db.get_reports(user_id)
    trend = an.consumption_trend([r["total_kwh"] for r in reversed(reports)])
    stack = get_stack(user_id)
    top = stack.peek()
    return {
        "user": public_user(user), "analysis": analysis, "tariff": tariff_summary(user),
        "reports": reports, "history": db.get_history(user_id), "trend": trend,
        "email_configured": utils.smtp_configured(),
        "can_undo_import": bool(top and top["type"] == "import"),
        "meta": {"categories": an.HOUSE_CATEGORIES, "machine_types": an.FACTORY_TYPES},
    }


def make_snapshot(dash):
    """The data stored with a report so its PDF can be regenerated later exactly as it was."""
    user, analysis = dash["user"], dash["analysis"]
    light = {k: v for k, v in analysis.items() if k not in ("hierarchy", "statistics")}
    return {"period": datetime.now().strftime("%B %Y"),
            "generated_at": datetime.now().strftime("%d %b %Y, %I:%M %p"),
            "user": {"name": user["name"], "email": user["email"]},
            "profile": {"consumer_type": user["consumer_type"], "billing_cycle": user["billing_cycle"],
                        "tariff_mode": user["tariff_mode"], "custom_rate": user["custom_rate"]},
            "analysis": light}


def save_current_report(dash):
    snapshot = make_snapshot(dash)
    analysis = dash["analysis"]
    highest = analysis["highest"]["name"] if analysis["highest"] else "None"
    db.save_report(dash["user"]["id"], snapshot["period"], analysis["cycle_kwh"],
                   analysis["bill"]["total"], analysis["connected_load_kw"], highest, json.dumps(snapshot))
    return snapshot


def email_in_background(user_id, snapshot, description):
    """
    THREADING: building the PDF and talking to the SMTP server can take a few seconds, so it
    runs in a background thread. The browser gets its answer immediately. Every error is caught
    and written to the log / update history, so a failure can never crash the server.
    """
    if not utils.smtp_configured():
        return "Email configuration is missing, so no email was sent. Your report was still generated."

    def worker():
        try:
            pdf_bytes = utils.build_pdf(snapshot)
            subject, body = utils.build_email(snapshot, description)
            sent, message = utils.send_email(snapshot["user"]["email"], subject, body, pdf_bytes)
            if not sent:
                utils.write_log(f"Email failed for user {user_id}: {message}")
                db.add_history(user_id, f"Report email could not be sent: {message}")
        except Exception:                                    # never let a thread crash silently
            utils.write_log("Background email error:\n" + traceback.format_exc())

    threading.Thread(target=worker, daemon=True).start()
    return f"The updated report is being emailed to {snapshot['user']['email']}."


def after_saved_change(user_id, description, send_email=False):
    """Steps 3-10 of the update workflow: history, recalculate, report, e-mail, fresh dashboard."""
    db.add_history(user_id, description)
    dash = build_dashboard(user_id)                          # recalculate everything
    snapshot = save_current_report(dash)                     # store report metadata
    dash["email_status"] = email_in_background(user_id, snapshot, description) if send_email else None
    dash["reports"] = db.get_reports(user_id)
    dash["history"] = db.get_history(user_id)
    return dash


# ---------------------------------------------------------------------------
# Error handling: friendly JSON, never a Python stack trace
# ---------------------------------------------------------------------------
@app.errorhandler(Exception)
def handle_error(error):
    if isinstance(error, HTTPException):
        messages = {404: "That address does not exist.", 405: "That method is not allowed here.",
                    413: "The uploaded file is too large (limit 5 MB)."}
        return fail(messages.get(error.code, error.description), error.code)
    utils.write_log("Unhandled error:\n" + traceback.format_exc())
    if isinstance(error, sqlite3.Error):
        return fail("A database error occurred. Please try again.", 500)
    return fail("Something went wrong on the server. Please try again.", 500)


# ---------------------------------------------------------------------------
# Serve the frontend (so the app works at http://127.0.0.1:5000)
# ---------------------------------------------------------------------------
@app.route("/")
def serve_index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/script.js")
def serve_script():
    return send_from_directory(FRONTEND_DIR, "script.js")


# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------
@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    confirm = str(data.get("confirm_password") or "")
    if not name or not email or not password or not confirm:
        return fail("Name, email, password and confirm password are all required.")
    if not utils.validate_person_name(name):
        return fail("Please enter a valid name (letters, spaces, . ' - only).")
    if not utils.validate_email(email):
        return fail("Please enter a valid email address.")
    valid, message = utils.validate_password(password)
    if not valid:
        return fail(message)
    if password != confirm:
        return fail("Password and confirm password do not match.")
    try:
        db.create_user(name, email, generate_password_hash(password))   # only the HASH is stored
    except sqlite3.IntegrityError:
        return fail("An account with this email already exists.", 409)
    return ok("Account created. Please log in.", {"email": email}, 201)


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    if not email or not password:
        return fail("Email and password are required.")
    user = db.get_user_by_email(email)
    if user is None or not check_password_hash(user["password_hash"], password):
        return fail("Incorrect email or password.", 401)
    session.clear()
    session["user_id"] = user["id"]
    session.permanent = True
    return ok(f"Welcome, {user['name']}!", public_user(user))


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return ok("You have been logged out.")


@app.route("/api/me", methods=["GET"])
@login_required
def me():
    return ok("Logged in.", public_user(g.user))


# ---------------------------------------------------------------------------
# PROFILE + TARIFF (billing cycle, consumer type, tariff mode, custom rate)
# ---------------------------------------------------------------------------
def describe_tariff(mode, rate):
    return f"Custom Rate {utils.format_inr(float(rate))}/unit" if mode == "custom" and rate else "Default Slab"


def read_tariff_fields(data, user):
    """Validate tariff input. Returns (updates, change_texts) or raises ValueError."""
    mode = str(data.get("tariff_mode", user["tariff_mode"])).strip().lower()
    if mode not in ("default", "custom"):
        raise ValueError("Tariff mode must be 'default' or 'custom'.")
    rate = user["custom_rate"]
    if mode == "custom":
        raw = data.get("custom_rate", user["custom_rate"])
        try:
            rate = float(raw)
        except (TypeError, ValueError):
            raise ValueError("Please enter a valid custom rate per unit.") from None
        if not rate > 0 or rate != rate:                    # rate must be > 0 (and not NaN)
            raise ValueError("Custom rate must be greater than zero.")
        if rate > 1000:
            raise ValueError("Custom rate looks too high. Please enter rupees per unit.")
    updates, changes = {}, []
    if mode != user["tariff_mode"] or (mode == "custom" and rate != user["custom_rate"]):
        updates = {"tariff_mode": mode, "custom_rate": rate}
        changes.append("Tariff changed: " + describe_tariff(user["tariff_mode"], user["custom_rate"])
                       + " \u2192 " + describe_tariff(mode, rate))
    return updates, changes


def apply_profile_changes(data, tariff_only=False):
    """Shared by PUT /api/profile and PUT /api/tariff."""
    user = g.user
    was_complete = bool(user["consumer_type"])
    updates, changes = {}, []
    try:
        if not tariff_only:
            if "consumer_type" in data:
                kind = str(data["consumer_type"]).strip().lower()
                if kind not in ("house", "factory"):
                    return fail("Consumer type must be House / Flat or Factory / Industrial.")
                if kind != user["consumer_type"]:
                    if was_complete and db.count_devices(user["id"]) > 0:
                        return fail("Delete all devices before changing the consumer type.")
                    updates["consumer_type"] = kind
                    changes.append("Consumer type set to " + utils.consumer_text(kind))
            elif not was_complete:
                return fail("Please choose a consumer type.")
            if "billing_cycle" in data:
                try:
                    cycle = int(data["billing_cycle"])
                except (TypeError, ValueError):
                    cycle = 0
                if cycle not in (1, 2):
                    return fail("Billing cycle must be 1 month or 2 months.")
                if cycle != user["billing_cycle"]:
                    updates["billing_cycle"] = cycle
                    changes.append(f"Billing cycle changed: {utils.cycle_text(user['billing_cycle']).lower()} "
                                   f"\u2192 {utils.cycle_text(cycle).lower()}")
        tariff_updates, tariff_changes = read_tariff_fields(data, user)
        updates.update(tariff_updates)
        changes.extend(tariff_changes)
    except ValueError as problem:
        return fail(str(problem))

    if not updates and was_complete:
        return ok("No changes to save.", build_dashboard(user["id"]))
    db.update_user(user["id"], **updates)
    if not was_complete:
        fresh = db.get_user_by_id(user["id"])
        description = (f"Electricity profile setup completed ({utils.consumer_text(fresh['consumer_type'])}, "
                       f"{utils.cycle_text(fresh['billing_cycle'])}, "
                       f"{utils.tariff_text(fresh['tariff_mode'], fresh['custom_rate'])})")
    else:
        description = "; ".join(changes)
    return ok("Settings saved.", after_saved_change(user["id"], description))


@app.route("/api/profile", methods=["PUT"])
@login_required
def update_profile():
    return apply_profile_changes(request.get_json(silent=True) or {})


@app.route("/api/tariff", methods=["GET"])
@login_required
def get_tariff():
    return ok("Tariff loaded.", tariff_summary(g.user))


@app.route("/api/tariff", methods=["PUT"])
@login_required
def update_tariff():
    blocked = profile_required()
    if blocked:
        return blocked
    return apply_profile_changes(request.get_json(silent=True) or {}, tariff_only=True)


# ---------------------------------------------------------------------------
# DASHBOARD / RECALCULATE
# ---------------------------------------------------------------------------
@app.route("/api/dashboard", methods=["GET"])
@login_required
def dashboard():
    blocked = profile_required()
    return blocked or ok("Dashboard loaded.", build_dashboard(g.user["id"]))


@app.route("/api/recalculate", methods=["POST"])
@login_required
def recalculate():
    """'Generate Report' button: recalculate and store the report (no email)."""
    blocked = profile_required()
    if blocked:
        return blocked
    dash = build_dashboard(g.user["id"])
    save_current_report(dash)
    dash["reports"] = db.get_reports(g.user["id"])
    return ok("Report generated.", dash)


# ---------------------------------------------------------------------------
# DEVICES
# ---------------------------------------------------------------------------
FIELD_NAMES = {"name": "name", "category": "type", "wattage": "wattage", "quantity": "quantity",
               "hours_per_day": "hours/day", "days_per_month": "days/month"}


def describe_edit(old, new):
    """Readable update-history text, e.g. 'AC usage changed: 6 hours -> 4 hours'."""
    changed = [key for key in FIELD_NAMES if old[key] != new[key]]
    if changed == ["hours_per_day"]:
        return f"{new['name']} usage changed: {old['hours_per_day']:g} hours \u2192 {new['hours_per_day']:g} hours"
    if not changed:
        return f"Saved {new['name']} (no changes)"
    parts = [f"{FIELD_NAMES[k]} {old[k]:g} \u2192 {new[k]:g}" if isinstance(new[k], (int, float))
             else f"{FIELD_NAMES[k]} {old[k]} \u2192 {new[k]}" for k in changed]
    return f"Edited {old['name']}: " + ", ".join(parts)


@app.route("/api/devices", methods=["GET"])
@login_required
def list_devices():
    blocked = profile_required()
    if blocked:
        return blocked
    return ok("Devices loaded.", build_dashboard(g.user["id"])["analysis"]["devices"])


@app.route("/api/devices", methods=["POST"])
@login_required
def add_device():
    blocked = profile_required()
    if blocked:
        return blocked
    clean, errors = utils.validate_device(request.get_json(silent=True) or {}, g.user["consumer_type"])
    if errors:
        return fail(" ".join(errors))
    kind = "machine" if g.user["consumer_type"] == "factory" else "appliance"
    device_id = db.add_device(g.user["id"], clean, kind)
    text = f"Added {clean['name']} - {clean['wattage']:g}W"
    get_stack(g.user["id"]).push({"type": "add", "text": text, "ids": [device_id]})
    label = "Machine" if kind == "machine" else "Appliance"
    return ok(f"{label} added successfully.", after_saved_change(g.user["id"], text), 201)


@app.route("/api/devices/<int:device_id>", methods=["PUT"])
@login_required
def edit_device(device_id):
    old = db.get_device(g.user["id"], device_id)               # also proves the device belongs to this user
    if old is None:
        return fail("Device not found.", 404)
    clean, errors = utils.validate_device(request.get_json(silent=True) or {}, g.user["consumer_type"])
    if errors:
        return fail(" ".join(errors))
    db.update_device(g.user["id"], device_id, clean)
    text = describe_edit(old, clean)
    get_stack(g.user["id"]).push({"type": "edit", "text": text, "ids": [device_id]})
    return ok("Saved successfully.", after_saved_change(g.user["id"], text))


@app.route("/api/devices/<int:device_id>", methods=["DELETE"])
@login_required
def remove_device(device_id):
    old = db.get_device(g.user["id"], device_id)
    if old is None:
        return fail("Device not found.", 404)
    db.delete_device(g.user["id"], device_id)
    text = f"Deleted {old['name']} - {old['wattage']:g}W"
    get_stack(g.user["id"]).push({"type": "delete", "text": text, "ids": []})
    return ok("Deleted successfully.", after_saved_change(g.user["id"], text))


@app.route("/api/suggest", methods=["GET"])
def suggest():
    """Trie autocomplete: /api/suggest?q=air&type=house"""
    trie = an.FACTORY_TRIE if request.args.get("type") == "factory" else an.HOUSE_TRIE
    return ok("Suggestions loaded.", trie.suggest(request.args.get("q", "")))


@app.route("/api/what-if", methods=["POST"])
@login_required
def what_if():
    """Temporary calculation. Nothing is written to the database here."""
    blocked = profile_required()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    try:
        device_id = utils.to_number(data.get("device_id"), "Device", whole=True)
        new_hours = utils.to_number(data.get("new_hours"), "New usage hours")
    except ValueError as problem:
        return fail(str(problem))
    if new_hours < 0 or new_hours > 24:
        return fail("New usage must be between 0 and 24 hours per day.")
    if db.get_device(g.user["id"], device_id) is None:
        return fail("Device not found.", 404)
    analyzer = an.ElectricityAnalyzer(g.user, db.get_devices(g.user["id"]))
    return ok("What-if calculated (nothing was saved).", analyzer.what_if(device_id, new_hours))


# ---------------------------------------------------------------------------
# CSV import / export, sample data, undo import
# ---------------------------------------------------------------------------
@app.route("/api/import-csv", methods=["POST"])
@login_required
def import_csv():
    blocked = profile_required()
    if blocked:
        return blocked
    if "file" in request.files:
        try:
            text = request.files["file"].read().decode("utf-8-sig")
        except UnicodeDecodeError:
            return fail("The file must be a UTF-8 text/CSV file.")
    else:
        text = str((request.get_json(silent=True) or {}).get("csv_text") or "")
    if not text.strip():
        return fail("Please choose a CSV file to import.")
    devices, errors = utils.parse_devices_csv(text, g.user["consumer_type"])
    if errors:                                               # all-or-nothing: fix the file and retry
        return fail(f"The CSV has {len(errors)} problem(s). Nothing was imported.", errors=errors[:50])
    kind = "machine" if g.user["consumer_type"] == "factory" else "appliance"
    ids = [db.add_device(g.user["id"], d, kind) for d in devices]
    text = f"Imported {len(ids)} devices from CSV"
    get_stack(g.user["id"]).push({"type": "import", "text": text, "ids": ids})
    return ok(f"{len(ids)} devices imported.", after_saved_change(g.user["id"], text), 201)


@app.route("/api/export-csv", methods=["GET"])
@login_required
def export_csv():
    blocked = profile_required()
    if blocked:
        return blocked
    content = utils.devices_to_csv(db.get_devices(g.user["id"]), g.user["consumer_type"])
    return Response(content, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=devices.csv"})


@app.route("/api/sample-data", methods=["POST"])
@login_required
def sample_data():
    blocked = profile_required()
    if blocked:
        return blocked
    if db.count_devices(g.user["id"]) > 0:
        return fail("Sample data can only be generated when you have no devices. Delete them first.", 409)
    kind = "machine" if g.user["consumer_type"] == "factory" else "appliance"
    ids = [db.add_device(g.user["id"], d, kind) for d in an.generate_sample_devices(g.user["consumer_type"])]
    text = f"Generated sample data ({len(ids)} devices)"
    get_stack(g.user["id"]).push({"type": "import", "text": text, "ids": ids})
    return ok("Sample data added.", after_saved_change(g.user["id"], text), 201)


@app.route("/api/undo-import", methods=["POST"])
@login_required
def undo_import():
    """Stack in action: only the MOST RECENT change can be undone, and only if it was an import."""
    stack = get_stack(g.user["id"])
    top = stack.peek()
    if not top or top["type"] != "import":
        return fail("The most recent change was not an import, so there is nothing to undo.", 409)
    stack.pop()
    removed = db.delete_devices(g.user["id"], top["ids"])
    return ok("Last import undone.", after_saved_change(g.user["id"], f"Undid last import ({removed} devices removed)"))


# ---------------------------------------------------------------------------
# REPORTS (list, view, PDF, e-mail)
# ---------------------------------------------------------------------------
@app.route("/api/reports", methods=["GET"])
@login_required
def list_reports():
    return ok("Reports loaded.", db.get_reports(g.user["id"]))


@app.route("/api/reports/<int:report_id>", methods=["GET"])
@login_required
def view_report(report_id):
    report = db.get_report(g.user["id"], report_id)
    if report is None:
        return fail("Report not found.", 404)
    return ok("Report loaded.", json.loads(report["snapshot_json"]))


def pdf_response(snapshot, name):
    try:
        pdf_bytes = utils.build_pdf(snapshot)
    except Exception:
        utils.write_log("PDF error:\n" + traceback.format_exc())
        return fail("The PDF report could not be generated.", 500)
    return Response(pdf_bytes, mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@app.route("/api/reports/<int:report_id>/pdf", methods=["GET"])
@login_required
def report_pdf(report_id):
    report = db.get_report(g.user["id"], report_id)           # regenerated from stored data
    if report is None:
        return fail("Report not found.", 404)
    name = "Electricity_Report_" + report["period"].replace(" ", "_") + ".pdf"
    return pdf_response(json.loads(report["snapshot_json"]), name)


@app.route("/api/reports/current/pdf", methods=["GET"])
@login_required
def current_pdf():
    blocked = profile_required()
    if blocked:
        return blocked
    snapshot = make_snapshot(build_dashboard(g.user["id"]))
    return pdf_response(snapshot, "Electricity_Report_Current.pdf")


@app.route("/api/email-report", methods=["POST"])
@login_required
def email_report():
    """Manual 'Email Report' button. Sent right away so the user sees the real result."""
    blocked = profile_required()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    if data.get("report_id"):
        report = db.get_report(g.user["id"], int(data["report_id"]))
        if report is None:
            return fail("Report not found.", 404)
        snapshot = json.loads(report["snapshot_json"])
    else:
        snapshot = make_snapshot(build_dashboard(g.user["id"]))
    if not utils.smtp_configured():
        return fail("Email configuration is missing. Set the SMTP environment variables and restart the server.", 503)
    try:
        pdf_bytes = utils.build_pdf(snapshot)
    except Exception:
        utils.write_log("PDF error:\n" + traceback.format_exc())
        return fail("The PDF report could not be generated.", 500)
    subject, body = utils.build_email(snapshot, "Report emailed on request")
    sent, message = utils.send_email(snapshot["user"]["email"], subject, body, pdf_bytes)
    return ok(message) if sent else fail(message, 502)


@app.route("/api/history", methods=["GET"])
@login_required
def history():
    return ok("History loaded.", db.get_history(g.user["id"], 50))


if __name__ == "__main__":
    if not os.environ.get("SECRET_KEY"):
        print("Note: SECRET_KEY is not set - using a temporary random key for this run.")
    if not utils.smtp_configured():
        print("Note: SMTP is not configured - reports will be generated but not emailed.")
    else:
        print("Note: SMTP email configured successfully.")
    port = int(os.environ.get("PORT", "5000"))
    print(f"Open http://127.0.0.1:{port} in your browser")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
