"""Goalie gear room: a small inventory app for a hockey club.

Tracks each piece of goalie equipment with a photo, shows who has it,
and keeps a history of checkouts. Built with Flask and SQLite.

Run it locally:
    python app.py
Then open http://127.0.0.1:5000
"""

import hashlib
import hmac
import os
import sqlite3
import uuid
from datetime import datetime, timedelta

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from PIL import Image, ImageOps

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE = os.path.join(BASE_DIR, "gear.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}

CATEGORIES = [
    "Pads",
    "Blocker",
    "Catcher",
    "Chest protector",
    "Pants",
    "Mask",
    "Skates",
    "Stick",
    "Other",
]
CONDITIONS = ["New", "Good", "Fair", "Worn"]

# The three states a piece of gear can be in. The key is stored in the
# database, the value is what people see on screen.
STATUS_LABELS = {
    "available": "Available",
    "out": "Checked out",
    "repair": "Needs repair",
}

app = Flask(__name__)
# The secret key signs the flash messages. Set a real one with an
# environment variable when you deploy. Never commit a real secret.
DEFAULT_SECRET_KEY = "dev-only-change-me"
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", DEFAULT_SECRET_KEY)
# The admin password also comes from an environment variable, never the code.
# If it is not set, nobody can log in and phone numbers stay masked for everyone.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
# An admin login lasts at most 8 hours, then you have to log in again.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
# Reject uploads larger than 16 MB.
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

os.makedirs(UPLOAD_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    category   TEXT NOT NULL,
    size       TEXT,
    condition  TEXT,
    notes      TEXT,
    photo      TEXT,
    status     TEXT NOT NULL DEFAULT 'available',
    held_by    TEXT,
    held_by_phone TEXT,
    out_since  TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkouts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES items(id),
    person      TEXT NOT NULL,
    phone       TEXT,
    out_at      TEXT NOT NULL,
    returned_at TEXT
);
"""


def get_db():
    """Open one database connection per request and reuse it."""
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row  # rows behave like dictionaries
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Columns added after the first version of the app. CREATE TABLE IF NOT EXISTS
# skips tables that already exist, so an older gear.db would never get these.
# Each one is added with ALTER TABLE only if it is missing, which keeps all
# existing rows. Old rows simply have no value (NULL) in the new column.
NEW_COLUMNS = [
    ("items", "held_by_phone", "TEXT"),
    ("checkouts", "phone", "TEXT"),
]


def add_missing_columns(db):
    for table, column, column_type in NEW_COLUMNS:
        existing = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
        if column not in existing:
            try:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
            except sqlite3.OperationalError as exc:
                # gunicorn starts several workers at once. If another worker
                # added the column a moment ago, that is fine.
                if "duplicate column" not in str(exc):
                    raise


def init_db():
    db = sqlite3.connect(DATABASE)
    db.executescript(SCHEMA)
    add_missing_columns(db)
    db.commit()
    db.close()


init_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


@app.template_filter("pretty_date")
def pretty_date(value):
    if not value:
        return ""
    return datetime.fromisoformat(value).strftime("%b %d, %Y")


@app.template_filter("days_since")
def days_since(value):
    if not value:
        return 0
    return (datetime.now() - datetime.fromisoformat(value)).days


@app.context_processor
def inject_lists():
    """Make these lists available in every template."""
    return {
        "CATEGORIES": CATEGORIES,
        "CONDITIONS": CONDITIONS,
        "STATUS_LABELS": STATUS_LABELS,
        "is_admin": is_admin(),
    }


def admin_fingerprint():
    """A one-way code made from the current admin password and secret key.

    The login cookie stores this code instead of just "admin = yes". If the
    password changes, the code changes, so every old login stops working.
    The password itself can't be worked out from the code.
    """
    message = ("admin:" + ADMIN_PASSWORD).encode()
    return hmac.new(app.config["SECRET_KEY"].encode(), message, hashlib.sha256).hexdigest()


def is_admin():
    if not ADMIN_PASSWORD:
        return False
    stored = session.get("admin")
    return isinstance(stored, str) and hmac.compare_digest(stored, admin_fingerprint())


@app.template_filter("phone")
def show_phone(value):
    """Show the full number to the admin and only the last 4 digits to everyone else.

    The masking happens here on the server, so the full number never reaches
    the browser of someone who is not logged in.
    """
    if not value:
        return ""
    if is_admin():
        return value
    digits = [ch for ch in value if ch.isdigit()]
    return "***-***-" + "".join(digits[-4:])


def get_item(item_id):
    item = get_db().execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)
    return item


def read_form():
    """Pull the item fields out of a submitted form and trim them."""
    return {
        "name": request.form.get("name", "").strip()[:100],
        "category": request.form.get("category", ""),
        "size": request.form.get("size", "").strip()[:30],
        "condition": request.form.get("condition", ""),
        "notes": request.form.get("notes", "").strip()[:1000],
    }


def validate(data):
    errors = []
    if not data["name"]:
        errors.append("Give the item a name.")
    if data["category"] not in CATEGORIES:
        errors.append("Pick a category.")
    if data["condition"] not in CONDITIONS:
        errors.append("Pick a condition.")
    return errors


PHONE_CHARACTERS = set("0123456789 +-().")


def valid_phone(phone):
    """Accept common phone formats like 555-123-4567 or +1 (555) 123 4567."""
    digits = sum(ch.isdigit() for ch in phone)
    return digits >= 7 and all(ch in PHONE_CHARACTERS for ch in phone)


def save_photo(file):
    """Validate, resize, and store an uploaded photo.

    Returns the new file name, or None if no file was chosen.
    Raises ValueError with a friendly message if the file is not usable.

    Re-saving every upload as a fresh JPEG with a random name means we never
    trust the original file name or contents.
    """
    if not file or not file.filename:
        return None

    extension = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError("Photos must be JPG, PNG, GIF, or WebP files.")

    try:
        image = Image.open(file.stream)
        image = ImageOps.exif_transpose(image)  # fix sideways phone photos
        image = image.convert("RGB")
        image.thumbnail((1200, 1200))  # keep the longest side at 1200 pixels
        filename = uuid.uuid4().hex + ".jpg"
        image.save(os.path.join(UPLOAD_DIR, filename), "JPEG", quality=85)
    except Exception as exc:
        raise ValueError("That file could not be read as an image.") from exc

    return filename


def delete_photo(filename):
    if not filename:
        return
    path = os.path.join(UPLOAD_DIR, filename)
    if os.path.exists(path):
        os.remove(path)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@app.get("/")
def index():
    category = request.args.get("category", "")
    status = request.args.get("status", "")

    sql = "SELECT * FROM items WHERE 1 = 1"
    params = []
    if category in CATEGORIES:
        sql += " AND category = ?"
        params.append(category)
    if status in STATUS_LABELS:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY category, name"

    db = get_db()
    items = db.execute(sql, params).fetchall()

    counts = {"available": 0, "out": 0, "repair": 0}
    for row in db.execute("SELECT status, COUNT(*) AS n FROM items GROUP BY status"):
        counts[row["status"]] = row["n"]

    return render_template(
        "index.html",
        items=items,
        counts=counts,
        total=sum(counts.values()),
        selected_category=category,
        selected_status=status,
    )


@app.get("/out")
def checked_out_list():
    items = get_db().execute(
        "SELECT * FROM items WHERE status = 'out' ORDER BY out_since"
    ).fetchall()
    return render_template("out.html", items=items)


@app.route("/item/new", methods=["GET", "POST"])
def new_item():
    if request.method == "GET":
        return render_template("form.html", item=None, editing=False)

    data = read_form()
    errors = validate(data)
    photo = None
    if not errors:
        try:
            photo = save_photo(request.files.get("photo"))
        except ValueError as exc:
            errors.append(str(exc))

    if errors:
        for message in errors:
            flash(message, "error")
        return render_template("form.html", item=data, editing=False)

    db = get_db()
    cursor = db.execute(
        "INSERT INTO items (name, category, size, condition, notes, photo, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            data["name"],
            data["category"],
            data["size"],
            data["condition"],
            data["notes"],
            photo,
            now_iso(),
        ),
    )
    db.commit()
    flash("Added " + data["name"] + ".", "ok")
    return redirect(url_for("item_detail", item_id=cursor.lastrowid))


@app.get("/item/<int:item_id>")
def item_detail(item_id):
    item = get_item(item_id)
    history = get_db().execute(
        "SELECT * FROM checkouts WHERE item_id = ? ORDER BY out_at DESC LIMIT 15",
        (item_id,),
    ).fetchall()
    return render_template("item.html", item=item, history=history)


@app.route("/item/<int:item_id>/edit", methods=["GET", "POST"])
def edit_item(item_id):
    item = get_item(item_id)
    if request.method == "GET":
        return render_template("form.html", item=item, editing=True)

    data = read_form()
    errors = validate(data)
    new_photo = None
    if not errors:
        try:
            new_photo = save_photo(request.files.get("photo"))
        except ValueError as exc:
            errors.append(str(exc))

    if errors:
        for message in errors:
            flash(message, "error")
        data["id"] = item["id"]
        data["photo"] = item["photo"]
        return render_template("form.html", item=data, editing=True)

    photo = item["photo"]
    if new_photo:
        photo = new_photo
    elif request.form.get("remove_photo"):
        photo = None

    db = get_db()
    db.execute(
        "UPDATE items SET name = ?, category = ?, size = ?, condition = ?,"
        " notes = ?, photo = ? WHERE id = ?",
        (
            data["name"],
            data["category"],
            data["size"],
            data["condition"],
            data["notes"],
            photo,
            item_id,
        ),
    )
    db.commit()

    if photo != item["photo"]:
        delete_photo(item["photo"])  # remove the old file once the update worked

    flash("Saved changes.", "ok")
    return redirect(url_for("item_detail", item_id=item_id))


@app.post("/item/<int:item_id>/checkout")
def checkout(item_id):
    item = get_item(item_id)
    person = request.form.get("person", "").strip()[:100]
    phone = request.form.get("phone", "").strip()[:30]

    if item["status"] != "available":
        flash("That item is not available to check out.", "error")
    elif not person:
        flash("Enter who is taking it.", "error")
    elif not valid_phone(phone):
        flash("Enter a contact phone number with at least 7 digits.", "error")
    else:
        now = now_iso()
        db = get_db()
        db.execute(
            "INSERT INTO checkouts (item_id, person, phone, out_at) VALUES (?, ?, ?, ?)",
            (item_id, person, phone, now),
        )
        db.execute(
            "UPDATE items SET status = 'out', held_by = ?, held_by_phone = ?,"
            " out_since = ? WHERE id = ?",
            (person, phone, now, item_id),
        )
        db.commit()
        flash("Checked out to " + person + ".", "ok")

    return redirect(url_for("item_detail", item_id=item_id))


@app.post("/item/<int:item_id>/return")
def return_item(item_id):
    item = get_item(item_id)

    if item["status"] != "out":
        flash("That item is not checked out.", "error")
    else:
        new_status = "repair" if request.form.get("needs_repair") else "available"
        db = get_db()
        db.execute(
            "UPDATE checkouts SET returned_at = ?"
            " WHERE item_id = ? AND returned_at IS NULL",
            (now_iso(), item_id),
        )
        db.execute(
            "UPDATE items SET status = ?, held_by = NULL, held_by_phone = NULL,"
            " out_since = NULL WHERE id = ?",
            (new_status, item_id),
        )
        db.commit()
        flash("Returned.", "ok")

    return redirect(url_for("item_detail", item_id=item_id))


@app.post("/item/<int:item_id>/repair")
def toggle_repair(item_id):
    item = get_item(item_id)

    if item["status"] == "available":
        new_status, message = "repair", "Marked as needing repair."
    elif item["status"] == "repair":
        new_status, message = "available", "Marked as repaired and available."
    else:
        flash("Return the gear before changing its repair status.", "error")
        return redirect(url_for("item_detail", item_id=item_id))

    db = get_db()
    db.execute("UPDATE items SET status = ? WHERE id = ?", (new_status, item_id))
    db.commit()
    flash(message, "ok")
    return redirect(url_for("item_detail", item_id=item_id))


@app.post("/item/<int:item_id>/delete")
def delete_item(item_id):
    item = get_item(item_id)
    db = get_db()
    db.execute("DELETE FROM checkouts WHERE item_id = ?", (item_id,))
    db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    db.commit()
    delete_photo(item["photo"])
    flash("Deleted " + item["name"] + ".", "ok")
    return redirect(url_for("index"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    if not ADMIN_PASSWORD or app.config["SECRET_KEY"] == DEFAULT_SECRET_KEY:
        # Without a real secret key, anyone could forge the login cookie.
        flash("Admin login is off. Set ADMIN_PASSWORD and SECRET_KEY first.", "error")
        return render_template("login.html")

    password = request.form.get("password", "")
    # compare_digest takes the same time whether the guess is close or not,
    # so an attacker cannot learn the password one character at a time.
    if hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
        session.clear()
        session["admin"] = admin_fingerprint()
        session.permanent = True  # so the 8-hour limit applies
        flash("Logged in as admin. Phone numbers are now visible.", "ok")
        return redirect(url_for("index"))

    flash("Wrong password.", "error")
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    flash("Logged out.", "ok")
    return redirect(url_for("index"))


@app.get("/uploads/<path:filename>")
def uploaded_file(filename):
    # send_from_directory refuses paths that try to escape the folder.
    return send_from_directory(UPLOAD_DIR, filename)


@app.errorhandler(413)
def too_large(_error):
    flash("That photo is too large. The limit is 16 MB.", "error")
    return redirect(request.referrer or url_for("index"))


if __name__ == "__main__":
    # Debug mode reloads on changes and shows detailed errors.
    # Use it only on your own computer, never on a public server.
    app.run(host="0.0.0.0", debug=False)
