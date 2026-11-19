import os
import random
import threading
import time
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request

import db

app = Flask(__name__)
app.json.ensure_ascii = False          # 接口直接返回中文，方便调试
app.json.sort_keys = False

FMT = "%Y-%m-%d %H:%M:%S"
QUIET_LABEL = {1: "可讨论", 2: "较安静", 3: "静音区"}
SOURCE_LABEL = {
    db.SRC_ACCESS: "门禁刷卡",
    db.SRC_SOCKET: "插座用电",
    db.SRC_CROWD: "众包上报",
    db.SRC_SYSTEM: "系统",
}


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def now_str():
    # 统一走东八区，别用裸 datetime.now()（线上服务器是 UTC，会差 8 小时）
    return db.now_cn().strftime(FMT)


def ensure_db():
    """首次运行时自动建表并灌入示例数据，保证 clone 下来直接能跑。"""
    db.init_db()
    if db.is_empty():
        import seed
        seed.build()
        seed.demo_reservations()
        print("已自动初始化示例数据")


def row_to_dict(row):
    return {k: row[k] for k in row.keys()}


def seat_rows(conn, room_id):
    return conn.execute(
        "SELECT * FROM seat WHERE room_id=? ORDER BY row_idx, col_idx", (room_id,)
    ).fetchall()


def room_stats(conn, room_id):
    """统计一间自习室的空位情况。"""
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN status='free' THEN 1 ELSE 0 END) AS free,
                  SUM(CASE WHEN status='reserved' THEN 1 ELSE 0 END) AS reserved,
                  SUM(CASE WHEN has_socket=1 THEN 1 ELSE 0 END) AS sockets
           FROM seat WHERE room_id=?""",
        (room_id,),
    ).fetchone()
    total = row["total"] or 0
    free = row["free"] or 0
    return {
        "total": total,
        "free": free,
        "reserved": row["reserved"] or 0,
        "occupied": total - free - (row["reserved"] or 0),
        "socket_seats": row["sockets"] or 0,
        "free_ratio": round(free / total, 3) if total else 0.0,
    }


def risk_level(free_ratio):
    """把"跑空风险"翻译成人话，对应文档里"把不确定性变成确定性"。"""
    if free_ratio >= 0.35:
        return "low", "去了就有座"
    if free_ratio >= 0.15:
        return "mid", "还有空位，早点去"
    return "high", "快满了，建议先预约"


def room_card(conn, room_row):
    st = room_stats(conn, room_row["id"])
    b = conn.execute(
        "SELECT b.name AS building, f.name AS floor FROM room r"
        " JOIN floor f ON f.id=r.floor_id JOIN building b ON b.id=f.building_id"
        " WHERE r.id=?",
        (room_row["id"],),
    ).fetchone()
    level, tip = risk_level(st["free_ratio"])
    return {
        "id": room_row["id"],
        "name": room_row["name"],
        "building": b["building"],
        "floor": b["floor"],
        "quiet": room_row["quiet"],
        "quiet_label": QUIET_LABEL.get(room_row["quiet"], ""),
        "has_socket": bool(room_row["has_socket"]),
        "tags": [t for t in (room_row["tags"] or "").split(",") if t],
        "stats": st,
        "risk": level,
        "risk_tip": tip,
    }


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# --------------------------------------------------------------------------
# API：筛选与地图
# --------------------------------------------------------------------------
@app.route("/api/meta")
def api_meta():
    """楼栋 + 楼层，给筛选栏用。"""
    conn = db.connect()
    try:
        buildings = []
        for b in conn.execute("SELECT * FROM building ORDER BY id"):
            floors = [
                {"id": f["id"], "name": f["name"]}
                for f in conn.execute(
                    "SELECT * FROM floor WHERE building_id=? ORDER BY level", (b["id"],)
                )
            ]
            agg = conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN s.status='free' THEN 1 ELSE 0 END) AS free
                   FROM seat s JOIN room r ON r.id=s.room_id
                   JOIN floor f ON f.id=r.floor_id
                   WHERE f.building_id=?""",
                (b["id"],),
            ).fetchone()
            total, free = agg["total"] or 0, agg["free"] or 0
            buildings.append({
                "id": b["id"],
                "name": b["name"],
                "campus": b["campus"],
                "open_hour": b["open_hour"],
                "close_hour": b["close_hour"],
                "floors": floors,
                "total": total,
                "free": free,
                "free_ratio": round(free / total, 3) if total else 0.0,
            })
        return jsonify({"buildings": buildings, "server_time": now_str()})
    finally:
        conn.close()


@app.route("/api/rooms")
def api_rooms():
    """按楼栋/楼层 + 插座/安静度/同行人数 筛选自习室。"""
    building_id = request.args.get("building_id", type=int)
    floor_id = request.args.get("floor_id", type=int)
    socket = request.args.get("socket")          # '1' 只看有插座
    quiet = request.args.get("quiet", type=int)  # 最低安静等级
    people = request.args.get("people", type=int, default=1)
    keyword = (request.args.get("q") or "").strip()

    sql = """SELECT r.* FROM room r
             JOIN floor f ON f.id = r.floor_id
             WHERE 1=1"""
    args = []
    if building_id:
        sql += " AND f.building_id=?"
        args.append(building_id)
    if floor_id:
        sql += " AND f.id=?"
        args.append(floor_id)
    if socket == "1":
        sql += " AND r.has_socket=1"
    if quiet:
        sql += " AND r.quiet>=?"
        args.append(quiet)
    if keyword:
        sql += " AND r.name LIKE ?"
        args.append(f"%{keyword}%")
    sql += " ORDER BY r.floor_id, r.id"

    conn = db.connect()
    try:
        cards = [room_card(conn, r) for r in conn.execute(sql, args)]
        # 同行人数：空位要够坐得下
        if people and people > 1:
            cards = [c for c in cards if c["stats"]["free"] >= people]
        # 空位多的排前面，一眼就能挑出"去了就有座"的
        cards.sort(key=lambda c: -c["stats"]["free_ratio"])
        return jsonify({"rooms": cards, "count": len(cards), "server_time": now_str()})
    finally:
        conn.close()


@app.route("/api/overview")
def api_overview():
    """首屏推荐：打开就出结果，不用注册不用等。"""
    conn = db.connect()
    try:
        agg = conn.execute(
            """SELECT COUNT(*) AS total, SUM(CASE WHEN status='free' THEN 1 ELSE 0 END) AS free
               FROM seat"""
        ).fetchone()
        total, free = agg["total"] or 0, agg["free"] or 0

        rooms = [room_card(conn, r) for r in conn.execute("SELECT * FROM room")]
        # 推荐打分：空位率为主，兼顾安静度与插座
        def score(c):
            return (c["stats"]["free_ratio"] * 0.55
                    + (c["quiet"] / 3) * 0.20
                    + (c["stats"]["socket_seats"] / max(c["stats"]["total"], 1)) * 0.15
                    + (0.10 if c["stats"]["free"] >= 10 else 0.0))

        rooms.sort(key=score, reverse=True)
        hot = conn.execute(
            """SELECT b.name AS building, f.name AS floor, r.name AS room,
                      COUNT(*) AS n
               FROM reservation v JOIN seat s ON s.id=v.seat_id
               JOIN room r ON r.id=s.room_id JOIN floor f ON f.id=r.floor_id
               JOIN building b ON b.id=f.building_id
               WHERE v.status='active' GROUP BY r.id ORDER BY n DESC LIMIT 3"""
        ).fetchall()

        return jsonify({
            "total": total,
            "free": free,
            "free_ratio": round(free / total, 3) if total else 0.0,
            "recommend": rooms[:5],
            "best": rooms[0] if rooms else None,
            "hot_reservations": [row_to_dict(h) for h in hot],
            "server_time": now_str(),
        })
    finally:
        conn.close()


@app.route("/api/rooms/<int:room_id>")
def api_room_detail(room_id):
    """座位图：前端每几秒拉一次，就是"实时空位地图"。"""
    conn = db.connect()
    try:
        room = conn.execute("SELECT * FROM room WHERE id=?", (room_id,)).fetchone()
        if not room:
            return jsonify({"error": "自习室不存在"}), 404

        seats = []
        for s in seat_rows(conn, room_id):
            seats.append({
                "id": s["id"],
                "code": s["code"],
                "row": s["row_idx"],
                "col": s["col_idx"],
                "has_socket": bool(s["has_socket"]),
                "status": s["status"],
                "source": SOURCE_LABEL.get(s["source"], s["source"]),
                "updated_at": s["updated_at"],
            })

        card = room_card(conn, room)
        card["grid_rows"] = room["grid_rows"]
        card["grid_cols"] = room["grid_cols"]
        return jsonify({"room": card, "seats": seats, "server_time": now_str()})
    finally:
        conn.close()


# --------------------------------------------------------------------------
# API：预约与提醒
# --------------------------------------------------------------------------
@app.route("/api/reserve", methods=["POST"])
def api_reserve():
    """一键预约。坐位被占了就拒绝，避免占座纠纷。"""
    data = request.get_json(silent=True) or {}
    seat_id = data.get("seat_id")
    user = (data.get("user") or "我").strip() or "我"
    minutes = int(data.get("minutes") or 120)
    if not seat_id:
        return jsonify({"ok": False, "msg": "缺少座位"}), 400

    conn = db.connect()
    try:
        seat = conn.execute("SELECT * FROM seat WHERE id=?", (seat_id,)).fetchone()
        if not seat:
            return jsonify({"ok": False, "msg": "座位不存在"}), 404
        if seat["status"] != db.FREE:
            # 会员特权：优先预约（演示用）
            if is_member(conn, user) and seat["status"] == db.OCCUPIED:
                pass
            else:
                return jsonify({"ok": False, "msg": "手慢了，这个座位刚被占"}), 409

        start = db.now_cn()
        end = start + timedelta(minutes=minutes)
        # 到点前 15 分钟提醒。立即开始的预约，提醒时间已过，下一轮轮询就会弹出来
        remind = start - timedelta(minutes=15)

        cur = conn.execute(
            "INSERT INTO reservation(seat_id, user_name, start_at, end_at, remind_at,"
            " status, people, created_at) VALUES (?,?,?,?,?, 'active', ?, ?)",
            (seat_id, user, start.strftime(FMT), end.strftime(FMT), remind.strftime(FMT),
             int(data.get("people") or 1), now_str()),
        )
        conn.execute(
            "UPDATE seat SET status=?, source=?, updated_at=? WHERE id=?",
            (db.RESERVED, db.SRC_SYSTEM, now_str(), seat_id),
        )
        conn.commit()
        return jsonify({
            "ok": True,
            "msg": "预约成功，到点前会提醒你",
            "reservation_id": cur.lastrowid,
            "end_at": end.strftime(FMT),
        })
    finally:
        conn.close()


@app.route("/api/reservations")
def api_reservations():
    user = request.args.get("user", "我")
    conn = db.connect()
    try:
        expire_finished(conn)
        rows = conn.execute(
            """SELECT v.*, s.code AS seat_code, s.has_socket,
                      r.name AS room, f.name AS floor, b.name AS building
               FROM reservation v
               JOIN seat s ON s.id = v.seat_id
               JOIN room r ON r.id = s.room_id
               JOIN floor f ON f.id = r.floor_id
               JOIN building b ON b.id = f.building_id
               WHERE v.user_name=? AND v.status='active'
               ORDER BY v.start_at""",
            (user,),
        ).fetchall()
        items, due = [], []
        now = db.now_cn()
        for r in rows:
            d = row_to_dict(r)
            d["has_socket"] = bool(d["has_socket"])
            end = datetime.strptime(d["end_at"], FMT)
            d["remain_minutes"] = max(0, int((end - now).total_seconds() // 60))
            items.append(d)
            if not d["reminded"] and datetime.strptime(d["remind_at"], FMT) <= now:
                due.append(d)
                conn.execute("UPDATE reservation SET reminded=1 WHERE id=?", (r["id"],))
        conn.commit()
        return jsonify({"reservations": items, "reminders": due, "server_time": now_str()})
    finally:
        conn.close()


@app.route("/api/reservations/<int:rid>/cancel", methods=["POST"])
def api_cancel(rid):
    conn = db.connect()
    try:
        r = conn.execute("SELECT * FROM reservation WHERE id=?", (rid,)).fetchone()
        if not r:
            return jsonify({"ok": False, "msg": "预约不存在"}), 404
        conn.execute("UPDATE reservation SET status='cancelled' WHERE id=?", (rid,))
        conn.execute(
            "UPDATE seat SET status=?, source=?, updated_at=? WHERE id=? AND status=?",
            (db.FREE, db.SRC_SYSTEM, now_str(), r["seat_id"], db.RESERVED),
        )
        conn.commit()
        return jsonify({"ok": True, "msg": "已取消，座位释放给其他同学"})
    finally:
        conn.close()


def expire_finished(conn):
    """到点没来的预约自动释放，减少"占着不走"。"""
    now = now_str()
    stale = conn.execute(
        "SELECT id, seat_id FROM reservation WHERE status='active' AND end_at < ?", (now,)
    ).fetchall()
    for s in stale:
        conn.execute("UPDATE reservation SET status='finished' WHERE id=?", (s["id"],))
        conn.execute(
            "UPDATE seat SET status=?, source=?, updated_at=? WHERE id=? AND status=?",
            (db.FREE, db.SRC_SYSTEM, now, s["seat_id"], db.RESERVED),
        )


# --------------------------------------------------------------------------
# API：众包上报 / 会员
# --------------------------------------------------------------------------
@app.route("/api/report", methods=["POST"])
def api_report():
    """同学看到座位实际情况，一键上报，立刻修正地图。"""
    data = request.get_json(silent=True) or {}
    seat_id = data.get("seat_id")
    claim = data.get("claim")  # free | occupied
    if not seat_id or claim not in ("free", "occupied"):
        return jsonify({"ok": False, "msg": "参数不对"}), 400

    conn = db.connect()
    try:
        seat = conn.execute("SELECT * FROM seat WHERE id=?", (seat_id,)).fetchone()
        if not seat:
            return jsonify({"ok": False, "msg": "座位不存在"}), 404
        conn.execute(
            "INSERT INTO report(seat_id, user_name, claim, note, created_at) VALUES (?,?,?,?,?)",
            (seat_id, (data.get("user") or "热心同学").strip() or "热心同学", claim,
             (data.get("note") or "").strip(), now_str()),
        )
        if seat["status"] != db.RESERVED:  # 不影响已锁定的预约
            conn.execute(
                "UPDATE seat SET status=?, source=?, updated_at=? WHERE id=?",
                (db.FREE if claim == "free" else db.OCCUPIED,
                 db.SRC_CROWD, now_str(), seat_id),
            )
        conn.commit()
        return jsonify({"ok": True, "msg": "感谢上报，地图已更新"})
    finally:
        conn.close()


@app.route("/api/reports")
def api_reports():
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT p.*, s.code AS seat_code, r.name AS room
               FROM report p JOIN seat s ON s.id=p.seat_id JOIN room r ON r.id=s.room_id
               ORDER BY p.id DESC LIMIT 20"""
        ).fetchall()
        return jsonify({"reports": [row_to_dict(r) for r in rows]})
    finally:
        conn.close()


def is_member(conn, user):
    row = conn.execute("SELECT * FROM member WHERE user_name=?", (user,)).fetchone()
    if not row or row["plan"] == "free":
        return False
    if row["expire_at"] and row["expire_at"] < now_str():
        return False
    return True


@app.route("/api/member", methods=["GET", "POST"])
def api_member():
    """9.9 元/月会员：优先预约 + 空位预测 + 常去楼栋订阅。"""
    if request.method == "GET":
        user = request.args.get("user", "我")
        conn = db.connect()
        try:
            m = is_member(conn, user)
            return jsonify({"is_member": m, "price": 9.9,
                            "benefits": ["优先预约锁定座位", "楼栋空位预测", "常去楼栋订阅推送",
                                         "预约时长延长至 4 小时"]})
        finally:
            conn.close()

    data = request.get_json(silent=True) or {}
    user = (data.get("user") or "我").strip() or "我"
    plan = data.get("plan", "free")
    conn = db.connect()
    try:
        expire = ((db.now_cn() + timedelta(days=30)).strftime(FMT)
                  if plan != "free" else None)
        conn.execute(
            "INSERT INTO member(user_name, plan, expire_at) VALUES (?,?,?)"
            " ON CONFLICT(user_name) DO UPDATE SET plan=excluded.plan, expire_at=excluded.expire_at",
            (user, plan, expire),
        )
        conn.commit()
        return jsonify({"ok": True, "is_member": plan != "free",
                        "msg": "已开通会员（演示）" if plan != "free" else "已退订"})
    finally:
        conn.close()


# --------------------------------------------------------------------------
# "实时"数据模拟：真实场景是门禁刷卡 + 插座用电网关推上来的
# --------------------------------------------------------------------------
def simulate_tick():
    conn = db.connect()
    try:
        expire_finished(conn)
        seats = conn.execute(
            "SELECT id, has_socket FROM seat WHERE status IN ('free','occupied')"
        ).fetchall()
        if not seats:
            return
        # 每次随机挑一小批座位，模拟有人来 / 有人走
        for s in random.sample(seats, k=max(1, len(seats) // 40)):
            new_status = db.OCCUPIED if s["has_socket"] and random.random() < 0.6 else \
                (db.OCCUPIED if random.random() < 0.5 else db.FREE)
            source = random.choice([db.SRC_ACCESS, db.SRC_SOCKET])
            conn.execute(
                "UPDATE seat SET status=?, source=?, updated_at=? WHERE id=? AND status IN ('free','occupied')",
                (new_status, source, now_str(), s["id"]),
            )
        conn.commit()
    except Exception as e:  # 模拟线程不该把主服务搞挂
        print("simulate_tick error:", e)
    finally:
        conn.close()


def start_simulator(interval=6):
    """启动"实时数据"模拟线程。

    ⚠️ 这个线程是跟着进程走的，所以线上必须保证**只有一个进程**在跑它。
    如果 gunicorn 开多 worker，每个 worker 都会各起一份模拟，
    结果是座位状态以 N 倍速乱跳，还会互相抢 SQLite 写锁。

    所以 render.yaml / Procfile 里把 worker 数固定成 1。
    这里的 RUN_SIMULATOR 是兜底开关：设成 0 就完全不跑模拟
    （比如以后把 simulate_tick 换成消费真实门禁数据流时，就用不上了）。
    """
    if os.environ.get("RUN_SIMULATOR", "1") != "1":
        print("RUN_SIMULATOR != 1，跳过模拟线程（座位状态将保持静态）")
        return None

    def loop():
        while True:
            time.sleep(interval)
            simulate_tick()

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


ensure_db()
start_simulator()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
