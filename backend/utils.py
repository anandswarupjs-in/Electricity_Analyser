"""
utils.py - helpers: validation, CSV files, charts, PDF, email, logging, socket/FTP demos.

Syllabus concepts shown here:
  * Regular expressions / pattern matching, string processing, conversions, exception handling
  * File processing: reading/writing CSV and text files, parsing, generator (yield)
  * Tables as lists, tuples, dictionaries, built-in functions, parameter passing (default args)
  * Matplotlib (chart in memory), ReportLab (PDF in memory)
  * Network programming: SMTP email, sockets, FTP (ftplib)
"""
import os
import re
import io
import csv
import math
import smtplib
import socket
import ssl
import threading
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from xml.sax.saxutils import escape

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

import matplotlib
matplotlib.use("Agg")  # Non-interactive backend safe for servers and threads
from matplotlib.figure import Figure                      # Figure (not pyplot) is thread-safe
from matplotlib.backends.backend_agg import FigureCanvasAgg
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "activity.log")

# ---------------------------------------------------------------------------
# REGULAR EXPRESSIONS (pattern matching)
# ---------------------------------------------------------------------------
EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
PERSON_PATTERN = re.compile(r"^[^\W\d_][\w .'\-]{0,59}$")             # starts with a letter
DEVICE_NAME_PATTERN = re.compile(r"^\w[\w .,'&()/+\-]{0,59}$")
WATTAGE_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(kw|w)?\s*$", re.IGNORECASE)   # 1500, 1500W, 1.5kW

MAX_CSV_ROWS = 5000

MESSAGES = {
    "email_footer": ("If you have added a new appliance/machine or want to change the usage duration, "
                     "please log in to the Electricity Consumption Analyser and update your information. "
                     "After saving the changes, your consumption, bill, website dashboard and report "
                     "will be updated."),
    "pdf_disclaimer": ("This is an approximate electricity consumption and bill estimate based on "
                       "user-provided appliance/machine ratings, usage durations and selected tariff "
                       "assumptions. Actual electricity bills may differ."),
}


# ---------------------------------------------------------------------------
# VALIDATION HELPERS
# ---------------------------------------------------------------------------
def validate_email(email):
    return bool(EMAIL_PATTERN.match(email or ""))


def validate_person_name(name):
    return bool(PERSON_PATTERN.match((name or "").strip()))


def validate_password(password):
    """Return (is_ok, message). Rule: at least 6 characters with a letter and a digit."""
    if len(password or "") < 6:
        return False, "Password must be at least 6 characters long."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return False, "Password must contain at least one letter and one number."
    return True, ""


def to_number(value, label, whole=False):
    """Convert text/number to int or float, raising ValueError with a friendly message."""
    text = "" if value is None else str(value).strip()
    if text == "":
        raise ValueError(f"{label} is required.")
    try:
        number = float(text)
    except ValueError:
        raise ValueError(f"{label} must be a number.") from None
    if math.isnan(number) or math.isinf(number):
        raise ValueError(f"{label} must be a number.")
    if whole and number != int(number):
        raise ValueError(f"{label} must be a whole number.")
    return int(number) if whole else number


def parse_wattage(value):
    """Accepts 1500, '1500', '1500W' or '1.5 kW' and returns watts as a float."""
    match = WATTAGE_PATTERN.match(str(value if value is not None else ""))
    if match:
        watts = float(match.group(1))
        return watts * 1000 if (match.group(2) or "").lower() == "kw" else watts
    return to_number(value, "Wattage")


def validate_device(data, consumer_type):
    """
    Check one device (from the form or a CSV row).
    Returns (clean_dict, errors_list). errors_list is empty when everything is valid.
    """
    errors, clean = [], {}
    factory = consumer_type == "factory"
    text_fields = (("name", "Machine name" if factory else "Appliance name"),
                   ("category", "Machine type" if factory else "Appliance category"))
    for key, label in text_fields:
        text = str(data.get(key) or "").strip()
        if not text:
            errors.append(f"{label} is required.")
        elif not DEVICE_NAME_PATTERN.match(text):
            errors.append(f"{label} has invalid characters or is longer than 60 characters.")
        else:
            clean[key] = text

    def check(key, label, parser, low, high, low_msg, high_msg):
        try:
            value = parser(data.get(key))
        except ValueError as problem:
            errors.append(str(problem))
            return
        if value < 0 and "negative" in low_msg:
            errors.append(low_msg)
        elif value < low:
            errors.append(low_msg)
        elif value > high:
            errors.append(high_msg)
        else:
            clean[key] = value

    check("wattage", "Wattage", parse_wattage, 0.0001, 10_000_000,
          "Wattage must be greater than zero.", "Wattage is too large.")
    check("quantity", "Quantity", lambda v: to_number(v, "Quantity", whole=True), 1, 100_000,
          "Quantity cannot be negative." if _is_negative(data.get("quantity")) else "Quantity must be at least 1.",
          "Quantity is too large.")
    check("hours_per_day", "Hours per day", lambda v: to_number(v, "Hours per day"), 0, 24,
          "Hours per day cannot be negative.", "Hours per day cannot be more than 24.")
    check("days_per_month", "Days per month", lambda v: to_number(v, "Days per month", whole=True), 1, 31,
          "Days per month cannot be negative." if _is_negative(data.get("days_per_month"))
          else "Days per month must be between 1 and 31.", "Days per month cannot be more than 31.")
    return clean, errors


def _is_negative(value):
    try:
        return float(str(value).strip()) < 0
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# NUMBER FORMATTING (string processing)
# ---------------------------------------------------------------------------
def format_inr(amount, symbol="\u20b9"):
    """5677.8 -> '₹5,677.80' using Indian digit grouping (12,34,567.00)."""
    sign = "-" if amount < 0 else ""
    whole, fraction = f"{abs(amount):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}{symbol}{whole}.{fraction}"


def cycle_text(months):
    return "2 Months" if months == 2 else "1 Month"


def consumer_text(consumer_type):
    return "Factory / Industrial" if consumer_type == "factory" else "House / Flat"


def tariff_text(mode, custom_rate):
    if mode == "custom" and custom_rate:
        return f"Custom Rate ({format_inr(float(custom_rate))} per unit)"
    return "Default Slab"


# ---------------------------------------------------------------------------
# CSV FILES: read (generator), validate, write
# ---------------------------------------------------------------------------
def csv_columns(consumer_type):
    second = "machine_type" if consumer_type == "factory" else "category"
    return ["name", second, "wattage", "quantity", "hours_per_day", "days_per_month"]


def read_csv_rows(text_stream, required_columns):
    """
    GENERATOR: yields (row_number, row_dict) ONE ROW AT A TIME, so a huge CSV file is
    never loaded into memory all at once. row_number is the line number in the file.
    """
    reader = csv.DictReader(text_stream)
    headers = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [col for col in required_columns if col not in headers]
    if missing:
        raise ValueError("CSV header is missing column(s): " + ", ".join(missing)
                         + ". Expected: " + ",".join(required_columns))
    reader.fieldnames = headers
    for row in reader:
        yield reader.line_num, row


def parse_devices_csv(file_text, consumer_type):
    """Parse + validate a CSV. Returns (valid_devices, error_messages). Nothing is saved here."""
    columns = csv_columns(consumer_type)
    devices, errors = [], []
    try:
        for count, (row_number, row) in enumerate(read_csv_rows(io.StringIO(file_text), columns), start=1):
            if count > MAX_CSV_ROWS:
                errors.append(f"Too many rows. The limit is {MAX_CSV_ROWS} rows per file.")
                break
            if not any((value or "").strip() for value in row.values() if isinstance(value, str)):
                continue                                              # skip blank lines
            data = {"name": row.get("name"), "category": row.get(columns[1]),
                    "wattage": row.get("wattage"), "quantity": row.get("quantity"),
                    "hours_per_day": row.get("hours_per_day"), "days_per_month": row.get("days_per_month")}
            clean, problems = validate_device(data, consumer_type)
            if problems:
                errors.extend(f"Row {row_number}: {message}" for message in problems)
            else:
                devices.append(clean)
    except csv.Error as problem:
        errors.append(f"The file is not a valid CSV: {problem}")
    except ValueError as problem:                                     # missing header columns
        errors.append(str(problem))
    if not devices and not errors:
        errors.append("The CSV file has no device rows.")
    return devices, errors


def devices_to_csv(devices, consumer_type):
    """Write device rows to CSV text (same columns that import accepts)."""
    columns = csv_columns(consumer_type)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for d in devices:
        writer.writerow([d["name"], d["category"], f"{d['wattage']:g}", d["quantity"],
                         f"{d['hours_per_day']:g}", d["days_per_month"]])
    return buffer.getvalue()


def devices_as_table(devices):
    """A table as a LIST OF LISTS: first row = header, then one list per device."""
    table = [["Device", "Wattage", "Qty", "Usage", "Monthly kWh", "Share"]]
    for d in devices:
        table.append([d["name"], f"{d['wattage']:g} W", d["quantity"],
                      f"{d['hours_per_day']:g} h/day x {d['days_per_month']} days",
                      f"{d['monthly_kwh']:.2f}", f"{d['percentage']:.1f}%"])
    return table


# ---------------------------------------------------------------------------
# TEXT FILE LOG (writing / reading a plain text file)
# ---------------------------------------------------------------------------
def write_log(message):
    """Append one line to activity.log. Never raises (logging must not break the app)."""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as log_file:
            log_file.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}\n")
    except OSError:
        pass


def read_log_tail(lines=20):
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as log_file:
            return log_file.readlines()[-lines:]
    except OSError:
        return []


# ---------------------------------------------------------------------------
# MATPLOTLIB: bar chart as PNG bytes (in memory, no temp files)
# ---------------------------------------------------------------------------
def create_bar_chart_png(devices, max_bars=12):
    top = devices[:max_bars]
    figure = Figure(figsize=(8, max(2.6, 0.45 * len(top) + 1.4)), dpi=110)
    FigureCanvasAgg(figure)
    axis = figure.add_subplot(111)
    if not top:
        axis.text(0.5, 0.5, "No devices added yet", ha="center", va="center")
        axis.axis("off")
    else:
        labels = [d["label"] for d in reversed(top)]
        values = [d["monthly_kwh"] for d in reversed(top)]
        bars = axis.barh(labels, values, color="#0f6b8f")
        biggest = max(values) or 1
        for bar, value in zip(bars, values):
            axis.text(bar.get_width() + biggest * 0.01, bar.get_y() + bar.get_height() / 2,
                      f"{value:g} kWh", va="center", fontsize=8)
        axis.set_xlim(0, biggest * 1.18)
        axis.set_xlabel("Monthly consumption (kWh)")
        axis.set_title("Monthly Consumption by Appliance / Machine", fontsize=11)
        axis.tick_params(axis="y", labelsize=8)
        for side in ("top", "right"):
            axis.spines[side].set_visible(False)
    figure.tight_layout()
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# REPORTLAB: PDF in memory
# ---------------------------------------------------------------------------
_fonts_ready = None


def _register_fonts():
    """Matplotlib ships DejaVu fonts that contain the rupee sign; the default PDF fonts do not."""
    global _fonts_ready
    if _fonts_ready is None:
        try:
            folder = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
            pdfmetrics.registerFont(TTFont("DejaVu", os.path.join(folder, "DejaVuSans.ttf")))
            pdfmetrics.registerFont(TTFont("DejaVu-Bold", os.path.join(folder, "DejaVuSans-Bold.ttf")))
            _fonts_ready = True
        except Exception:
            _fonts_ready = False
    return _fonts_ready


def build_pdf(snapshot):
    """Create the report PDF from a snapshot dict and return it as bytes."""
    fonts = _register_fonts()
    regular, bold = ("DejaVu", "DejaVu-Bold") if fonts else ("Helvetica", "Helvetica-Bold")
    symbol = "\u20b9" if fonts else "Rs. "
    money = lambda amount: format_inr(amount, symbol)

    a, user, profile = snapshot["analysis"], snapshot["user"], snapshot["profile"]
    bill, months = a["bill"], a["months"]

    title = ParagraphStyle("t", fontName=bold, fontSize=18, textColor=colors.HexColor("#0b3a53"), leading=22, spaceAfter=6)
    heading = ParagraphStyle("h", fontName=bold, fontSize=12, textColor=colors.HexColor("#0b3a53"),
                             spaceBefore=12, spaceAfter=5)
    body = ParagraphStyle("b", fontName=regular, fontSize=9, leading=12)
    small = ParagraphStyle("s", fontName=regular, fontSize=8, leading=10, textColor=colors.HexColor("#555555"))

    def make_table(rows, widths, header=True):
        wrapped = [[Paragraph(escape(str(cell)), body) for cell in row] for row in rows]
        table = Table(wrapped, colWidths=widths, repeatRows=1 if header else 0)
        style = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b8c4cc")),
                 ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
        if header:
            style += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dcebf2"))]
        table.setStyle(TableStyle(style))
        return table

    story = [Paragraph("ELECTRICITY CONSUMPTION REPORT", title),
             Paragraph(f"Period: {escape(snapshot['period'])} &nbsp;|&nbsp; Generated: {escape(snapshot['generated_at'])}", small),
             Spacer(1, 8)]

    rate_info = ("Default slab tariff (telescopic billing - see breakdown below)" if bill["mode"] == "default"
                 else f"{money(float(profile['custom_rate']))} per unit on all units")
    story.append(make_table([
        ["Customer", user["name"]], ["Email", user["email"]],
        ["Consumer Type", consumer_text(profile["consumer_type"])],
        ["Billing Cycle", cycle_text(months)],
        ["Tariff", tariff_text(profile["tariff_mode"], profile["custom_rate"])],
        ["Rate information", rate_info]], [4.2 * cm, 12.8 * cm], header=False))

    story.append(Paragraph("CONSUMPTION SUMMARY", heading))
    summary = [["Total Consumption (" + cycle_text(months) + ")", f"{a['cycle_kwh']:g} kWh"]]
    if months == 2:
        summary.append(["Monthly average", f"{a['monthly_average_kwh']:g} kWh"])
    summary += [["Average Daily Consumption", f"{a['avg_daily_kwh']:g} kWh"],
                ["Connected Load", f"{a['connected_load_kw']:g} kW"],
                ["Approximate Bill", money(bill["total"])]]
    if months == 2:
        summary.append(["Average monthly bill", money(bill["monthly_average"])])
    if a["highest"]:
        summary.append(["Highest Consumer", f"{a['highest']['name']} ({a['highest']['percentage']:.1f}%)"])
    story.append(make_table(summary, [8 * cm, 9 * cm], header=False))

    story.append(Paragraph("BILL BREAKDOWN", heading))
    story.append(make_table([["Slab (units)", "Units", "Rate / unit", "Amount"]] +
                            [[b["label"], f"{b['units']:g}", money(b["rate"]), money(b["amount"])]
                             for b in bill["breakdown"]], [5 * cm, 3.5 * cm, 4 * cm, 4.5 * cm]))

    story.append(Paragraph("DEVICE-WISE CONSUMPTION", heading))
    if a["devices"]:
        story.append(make_table(devices_as_table(a["devices"]),
                                [4.2 * cm, 2.2 * cm, 1.4 * cm, 4.6 * cm, 2.6 * cm, 2 * cm]))
    else:
        story.append(Paragraph("No devices have been added yet.", body))

    png = create_bar_chart_png(a["devices"])
    width_px, height_px = ImageReader(io.BytesIO(png)).getSize()
    story.append(KeepTogether([Paragraph("BAR CHART", heading),      # keep heading with its chart
                               Image(io.BytesIO(png), width=17 * cm, height=17 * cm * height_px / width_px)]))

    story.append(Paragraph("RECOMMENDATIONS", heading))
    for rec in a["recommendations"][:8]:
        story.append(Paragraph("&bull; " + escape(rec["text"]), body))

    story.append(Paragraph("DISCLAIMER", heading))
    story.append(Paragraph(MESSAGES["pdf_disclaimer"], small))

    buffer = io.BytesIO()
    SimpleDocTemplate(buffer, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm,
                      topMargin=1.8 * cm, bottomMargin=1.8 * cm,
                      title="Electricity Consumption Report").build(story)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# EMAIL (SMTP) - credentials come from environment variables, never from the code
# ---------------------------------------------------------------------------
def smtp_settings():
    host = os.environ.get("SMTP_HOST", "")
    port_val = os.environ.get("SMTP_PORT") or "587"
    username = os.environ.get("SMTP_USERNAME", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM") or username
    sender_name = os.environ.get("SMTP_FROM_NAME", "")

    clean_pass = password.strip()
    if len(clean_pass.replace(" ", "")) == 16:
        clean_pass = clean_pass.replace(" ", "")

    return {
        "host": host.strip(),
        "port": int(port_val),
        "username": username.strip(),
        "password": clean_pass,
        "sender": sender.strip(),
        "sender_name": sender_name.strip(),
    }


def smtp_configured():
    settings = smtp_settings()
    return all([settings["host"], settings["username"], settings["password"], settings["sender"]])


def build_email(snapshot, change_description=None):
    """Return (subject, body_text) for the update email."""
    a, user, profile = snapshot["analysis"], snapshot["user"], snapshot["profile"]
    highest = a["highest"]["name"] if a["highest"] else "No devices added yet"
    lines = [f"Dear {user['name']},", "",
             "Your electricity consumption report has been updated.", ""]
    if change_description:
        lines += [f"Latest change: {change_description}", ""]
    lines += ["Billing Cycle:", cycle_text(a["months"]), "",
              "Consumer Type:", consumer_text(profile["consumer_type"]), "",
              "Estimated Consumption:", f"{a['cycle_kwh']:g} kWh", "",
              "Approximate Bill:", format_inr(a["bill"]["total"]), "",
              "Highest Consumer:", highest, "",
              "The detailed report is attached as a PDF.", "",
              "The report includes:", "- appliance/machine consumption", "- bar chart",
              "- estimated bill", "- connected load", "- recommendations", "",
              "This is an approximate estimate. Actual electricity bills may vary.", "",
              MESSAGES["email_footer"]]
    return f"Electricity Consumption Report - {snapshot['period']}", "\n".join(lines)


def send_email(to_address, subject, body, pdf_bytes=None, pdf_name="Electricity_Report.pdf"):
    """Send an email with an optional PDF. Returns (success, message) and never raises."""
    if not smtp_configured():
        return False, ("Email configuration is missing (set SMTP_HOST, SMTP_PORT, SMTP_USERNAME, "
                       "SMTP_PASSWORD and SMTP_FROM).")
    settings = smtp_settings()
    message = EmailMessage()
    message["Subject"], message["To"] = subject, to_address
    message["From"] = formataddr((settings["sender_name"], settings["sender"])) if settings["sender_name"] else settings["sender"]
    message.set_content(body)
    if pdf_bytes:
        message.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=pdf_name)
    try:
        if settings["port"] == 465:
            with smtplib.SMTP_SSL(settings["host"], 465, timeout=20, context=ssl.create_default_context()) as server:
                server.login(settings["username"], settings["password"])
                server.send_message(message)
        else:
            with smtplib.SMTP(settings["host"], settings["port"], timeout=20) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(settings["username"], settings["password"])
                server.send_message(message)
        return True, f"Email sent to {to_address}."
    except smtplib.SMTPAuthenticationError:
        return False, "The email server rejected the username or password."
    except (smtplib.SMTPException, OSError) as problem:
        return False, f"The email could not be sent: {problem}"


# ---------------------------------------------------------------------------
# NETWORK DEMOS (not used by the web app; run `python utils.py` to try the socket demo)
# ---------------------------------------------------------------------------
def socket_echo_demo(message="hello from client"):
    """Minimal local TCP demo: a server thread upper-cases whatever the client sends."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))                 # port 0 = let the OS choose a free port
    server.listen(1)
    port = server.getsockname()[1]

    def serve_once():
        connection, _ = server.accept()
        with connection:
            connection.sendall(connection.recv(1024).upper())

    worker = threading.Thread(target=serve_once, daemon=True)
    worker.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
            client.sendall(message.encode("utf-8"))
            return client.recv(1024).decode("utf-8")
    finally:
        worker.join(timeout=2)
        server.close()


def ftp_backup_demo(host, username, password, local_path, remote_name=None):
    """
    FTP CONCEPT: how a database backup would be uploaded with ftplib.
    It is NEVER called automatically, and credentials must be passed in by the caller.
    """
    from ftplib import FTP
    remote_name = remote_name or os.path.basename(local_path)
    with FTP(host, timeout=15) as ftp:
        ftp.login(username, password)
        with open(local_path, "rb") as backup_file:
            ftp.storbinary(f"STOR {remote_name}", backup_file)
    return f"Uploaded {remote_name} to {host}"


if __name__ == "__main__":
    print("Socket demo reply:", socket_echo_demo("electricity analyser"))
