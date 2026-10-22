"""MentorBridge - mentor/mentee platform: auto-booking, embedded video call + chat."""
import os, secrets, datetime as dt
from functools import wraps
import pymysql
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, abort, Response)
from werkzeug.security import generate_password_hash, check_password_hash
from flask_socketio import SocketIO, join_room, emit

app = Flask(__name__)
app.config.update(SECRET_KEY=os.getenv("SECRET_KEY", "change-me"),
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
sio = SocketIO(app, async_mode="threading")
DB = dict(host=os.getenv("DB_HOST", "localhost"), port=int(os.getenv("DB_PORT", "3306")), user=os.getenv("DB_USER", "root"),
          password=os.getenv("DB_PASSWORD", ""), database=os.getenv("DB_NAME", "mentorbridge"),
          cursorclass=pymysql.cursors.DictCursor, autocommit=True, charset="utf8mb4")
if os.getenv("DB_SSL") == "1":   # cloud MySQL providers usually require TLS
    DB["ssl"] = {"ca": os.getenv("DB_SSL_CA", "/etc/ssl/certs/ca-certificates.crt")}
OPEN_ROOMS = os.getenv("DEV_OPEN_ROOMS") == "1"   # testing: skip the time-window check


def run(sql, args=(), one=False, write=False):
    """One short-lived connection per query (swap for a pool in production)."""
    con = pymysql.connect(**DB)
    try:
        with con.cursor() as c:
            c.execute(sql, args)
            if write:
                return c.lastrowid
            return c.fetchone() if one else c.fetchall()
    finally:
        con.close()


def login_required(role=None):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if "uid" not in session:
                return redirect(url_for("login"))
            if role and session.get("role") != role:
                abort(403)
            return f(*a, **k)
        return w
    return deco


# ---------- scheduling engine ----------
def free_slots(mid, days=14):
    p = run("SELECT slot_minutes FROM mentor_profiles WHERE user_id=%s", (mid,), one=True)
    if not p:
        return []
    step = dt.timedelta(minutes=p["slot_minutes"])
    now = dt.datetime.now() + dt.timedelta(hours=2)          # 2h minimum notice
    until = now + dt.timedelta(days=days)
    taken = {r["start_at"] for r in run(
        "SELECT start_at FROM bookings WHERE mentor_id=%s AND status='confirmed' AND start_at>=%s", (mid, now))}
    windows = run("SELECT * FROM availability WHERE mentor_id=%s", (mid,))
    out = set()
    for d in range(days + 1):
        day = (now + dt.timedelta(days=d)).date()
        midnight = dt.datetime.combine(day, dt.time())
        for w in windows:
            if w["weekday"] != day.weekday():
                continue
            t, end = midnight + w["start_time"], midnight + w["end_time"]   # TIME -> timedelta
            while t + step <= end:
                if now <= t <= until and t not in taken:
                    out.add(t)
                t += step
    return sorted(out)


def book(mid, uid, start, topic):
    """Atomic booking. Returns booking id, or None if the slot is gone / invalid."""
    if start not in free_slots(mid):
        return None
    if run("SELECT id FROM bookings WHERE mentee_id=%s AND status='confirmed' AND start_at=%s", (uid, start), one=True):
        return None
    step = run("SELECT slot_minutes FROM mentor_profiles WHERE user_id=%s", (mid,), one=True)["slot_minutes"]
    try:
        return run("""INSERT INTO bookings(mentor_id,mentee_id,start_at,end_at,topic,room_token,slot_key)
                      VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                   (mid, uid, start, start + dt.timedelta(minutes=step), topic[:255],
                    secrets.token_hex(16), f"{mid}|{start:%Y%m%d%H%M}"), write=True)
    except pymysql.err.IntegrityError:        # someone else got there first
        return None


# ---------- auth ----------
@app.route("/")
def home():
    return redirect(url_for("mentors") if "uid" in session else url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form
        role = f.get("role") if f.get("role") in ("mentee", "mentor") else "mentee"
        if len(f.get("password", "")) < 8:
            flash("Use a password of at least 8 characters.")
        elif run("SELECT id FROM users WHERE email=%s", (f["email"].lower(),), one=True):
            flash("That email is already registered. Log in instead.")
        else:
            uid = run("INSERT INTO users(name,email,password_hash,role) VALUES(%s,%s,%s,%s)",
                      (f["name"].strip(), f["email"].lower(), generate_password_hash(f["password"]), role), write=True)
            if role == "mentor":
                run("INSERT INTO mentor_profiles(user_id) VALUES(%s)", (uid,), write=True)
            session.update(uid=uid, role=role, name=f["name"].strip())
            return redirect(url_for("dashboard"))
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = run("SELECT * FROM users WHERE email=%s", (request.form["email"].lower(),), one=True)
        if u and check_password_hash(u["password_hash"], request.form["password"]):
            session.clear()
            session.update(uid=u["id"], role=u["role"], name=u["name"])
            return redirect(url_for("dashboard"))
        flash("Email or password is incorrect.")
    return render_template("auth.html", mode="login")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- discovery + booking ----------
@app.route("/mentors")
@login_required()
def mentors():
    kw = f"%{request.args.get('q', '').strip()}%"
    rows = run("""SELECT u.id,u.name,p.* FROM users u JOIN mentor_profiles p ON p.user_id=u.id
                  WHERE p.headline<>'' AND (p.industry LIKE %s OR p.skills LIKE %s OR p.headline LIKE %s OR u.name LIKE %s)
                  ORDER BY p.years_exp DESC""", (kw,) * 4)
    for r in rows:
        s = free_slots(r["id"], 14)
        r["next_slot"] = s[0] if s else None
    return render_template("mentors.html", mentors=rows, q=request.args.get("q", ""))


@app.route("/mentor/<int:mid>")
@login_required()
def mentor(mid):
    m = run("SELECT u.id,u.name,p.* FROM users u JOIN mentor_profiles p ON p.user_id=u.id WHERE u.id=%s", (mid,), one=True)
    if not m:
        abort(404)
    days = {}
    for s in free_slots(mid):
        days.setdefault(s.date(), []).append(s)
    return render_template("mentor.html", m=m, days=days)


@app.route("/book/<int:mid>", methods=["POST"])
@login_required("mentee")
def book_slot(mid):
    try:
        start = dt.datetime.strptime(request.form["start"], "%Y-%m-%d %H:%M")
    except (KeyError, ValueError):
        abort(400)
    bid = book(mid, session["uid"], start, request.form.get("topic", ""))
    flash("Session confirmed." if bid else "That slot was just taken. Pick another time.")
    return redirect(url_for("dashboard" if bid else "mentor", mid=mid))


@app.route("/auto", methods=["POST"])
@login_required("mentee")
def auto():
    """Auto-booking: earliest free slot across all mentors matching a skill/industry and time of day."""
    kw, part = f"%{request.form.get('keyword', '').strip()}%", request.form.get("part", "any")
    lo, hi = {"morning": (0, 12), "afternoon": (12, 17), "evening": (17, 24)}.get(part, (0, 24))
    best = None
    for m in run("SELECT user_id FROM mentor_profiles WHERE headline<>'' AND (industry LIKE %s OR skills LIKE %s OR headline LIKE %s)", (kw,) * 3):
        for s in free_slots(m["user_id"]):
            if lo <= s.hour < hi:
                if not best or s < best[1]:
                    best = (m["user_id"], s)
                break
    if best and book(best[0], session["uid"], best[1], request.form.get("topic", "")):
        flash("Matched and booked with the earliest available mentor.")
        return redirect(url_for("dashboard"))
    flash("No open slot matched. Try a broader keyword or another time of day.")
    return redirect(url_for("mentors"))


# ---------- dashboard / mentor tools ----------
@app.route("/dashboard")
@login_required()
def dashboard():
    col, other = ("mentor_id", "mentee_id") if session["role"] == "mentor" else ("mentee_id", "mentor_id")
    rows = run(f"""SELECT b.*, u.name AS other_name FROM bookings b JOIN users u ON u.id=b.{other}
                   WHERE b.{col}=%s AND b.status='confirmed' ORDER BY b.start_at""", (session["uid"],))
    ctx = dict(bookings=rows, now=dt.datetime.now())
    if session["role"] == "mentor":
        ctx["p"] = run("SELECT * FROM mentor_profiles WHERE user_id=%s", (session["uid"],), one=True)
        ctx["av"] = run("SELECT * FROM availability WHERE mentor_id=%s ORDER BY weekday,start_time", (session["uid"],))
    return render_template("dashboard.html", **ctx)


@app.route("/mentor/profile", methods=["POST"])
@login_required("mentor")
def save_profile():
    f = request.form
    run("""UPDATE mentor_profiles SET headline=%s,industry=%s,skills=%s,bio=%s,years_exp=%s,calendly_url=%s,slot_minutes=%s
           WHERE user_id=%s""", (f["headline"], f["industry"], f["skills"], f["bio"], int(f.get("years_exp") or 0),
                                 f.get("calendly_url") or None, int(f.get("slot_minutes") or 30), session["uid"]), write=True)
    flash("Profile saved.")
    return redirect(url_for("dashboard"))


@app.route("/mentor/availability", methods=["POST"])
@login_required("mentor")
def add_availability():
    f = request.form
    if f["start"] < f["end"]:
        run("INSERT INTO availability(mentor_id,weekday,start_time,end_time) VALUES(%s,%s,%s,%s)",
            (session["uid"], int(f["weekday"]), f["start"], f["end"]), write=True)
    else:
        flash("End time must be after start time.")
    return redirect(url_for("dashboard"))


@app.route("/mentor/availability/<int:aid>/delete", methods=["POST"])
@login_required("mentor")
def del_availability(aid):
    run("DELETE FROM availability WHERE id=%s AND mentor_id=%s", (aid, session["uid"]), write=True)
    return redirect(url_for("dashboard"))


def _own_booking(bid):
    b = run("SELECT * FROM bookings WHERE id=%s", (bid,), one=True)
    if not b or session["uid"] not in (b["mentor_id"], b["mentee_id"]):
        abort(404)
    return b


@app.route("/booking/<int:bid>/cancel", methods=["POST"])
@login_required()
def cancel(bid):
    _own_booking(bid)
    run("UPDATE bookings SET status='cancelled', slot_key=NULL WHERE id=%s", (bid,), write=True)
    flash("Session cancelled. The slot is open again.")
    return redirect(url_for("dashboard"))


@app.route("/booking/<int:bid>/notes", methods=["POST"])
@login_required("mentor")
def notes(bid):
    b = _own_booking(bid)
    if b["mentor_id"] == session["uid"]:
        run("UPDATE bookings SET notes=%s WHERE id=%s", (request.form["notes"], bid), write=True)
    return redirect(url_for("dashboard"))


@app.route("/booking/<int:bid>.ics")
@login_required()
def ics(bid):
    b = _own_booking(bid)
    f = "%Y%m%dT%H%M%S"
    body = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//MentorBridge//EN\r\nBEGIN:VEVENT\r\n"
            f"UID:{b['room_token']}@mentorbridge\r\nDTSTART;TZID=Asia/Kolkata:{b['start_at']:{f}}\r\n"
            f"DTEND;TZID=Asia/Kolkata:{b['end_at']:{f}}\r\nSUMMARY:Mentoring session\r\n"
            f"URL:{url_for('room', token=b['room_token'], _external=True)}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    return Response(body, mimetype="text/calendar", headers={"Content-Disposition": "attachment; filename=session.ics"})


# ---------- video room + chat ----------
def room_booking(token):
    b = run("SELECT * FROM bookings WHERE room_token=%s AND status='confirmed'", (token,), one=True)
    if not b or session.get("uid") not in (b["mentor_id"], b["mentee_id"]):
        return None
    now = dt.datetime.now()
    if not OPEN_ROOMS and not (b["start_at"] - dt.timedelta(minutes=10) <= now <= b["end_at"] + dt.timedelta(minutes=30)):
        return None
    return b


@app.route("/room/<token>")
@login_required()
def room(token):
    b = room_booking(token)
    if not b:
        flash("This room opens 10 minutes before the session, for the two people booked.")
        return redirect(url_for("dashboard"))
    return render_template("room.html", token=token, me=session["name"])


@sio.on("join")
def on_join(token):
    b = room_booking(token)
    if not b:
        return emit("denied")
    join_room(token)
    hist = run("""SELECT u.name, m.body FROM messages m JOIN users u ON u.id=m.sender_id
                  WHERE m.booking_id=%s ORDER BY m.id""", (b["id"],))
    emit("history", hist)
    emit("peer-joined", room=token, include_self=False)


@sio.on("chat")
def on_chat(data):
    b = room_booking(data.get("token"))
    body = (data.get("body") or "").strip()[:2000]
    if b and body:
        run("INSERT INTO messages(booking_id,sender_id,body) VALUES(%s,%s,%s)", (b["id"], session["uid"], body), write=True)
        emit("chat", {"name": session["name"], "body": body}, room=data["token"])


@sio.on("signal")   # relays WebRTC offer/answer/ICE between the two participants
def on_signal(data):
    if room_booking(data.get("token")):
        emit("signal", data["payload"], room=data["token"], include_self=False)


if __name__ == "__main__":
    sio.run(app, host="0.0.0.0", port=5000, debug=True, allow_unsafe_werkzeug=True)
