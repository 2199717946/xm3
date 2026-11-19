"""生成示例数据：楼栋 / 楼层 / 自习室 / 座位。

真实场景下座位状态来自门禁与插座网关，这里按时间段模拟出一个合理的初始占用率
（上课时段空位多、晚高峰空位紧张），让原型看起来是"活"的。
"""
import random
from datetime import timedelta

import db

random.seed(20261119)

# (楼栋, 楼层, 自习室, 行, 列, 安静度, 是否有插座, 标签)
LAYOUT = [
    ("图书馆", "LIB", "东区", [
        ("1F", [
            ("中文图书阅览室", 6, 10, 3, 1, "静音区,有台灯"),
            ("电子阅览室", 5, 8, 3, 1, "配电脑,可打印"),
            ("期刊阅览区", 4, 8, 2, 1, "可小声交流"),
        ]),
        ("2F", [
            ("考研自习室 A", 7, 10, 3, 1, "静音区,长期占座多"),
            ("考研自习室 B", 7, 10, 3, 1, "静音区"),
            ("小组讨论间", 4, 6, 1, 0, "可讨论,需预约"),
        ]),
        ("3F", [
            ("安静自习区", 8, 10, 3, 1, "静音区,采光好"),
            ("外文阅览室", 5, 8, 3, 1, "静音区"),
        ]),
    ]),
    ("第一教学楼", "A", "东区", [
        ("1F", [
            ("A101 自习室", 6, 8, 2, 1, "离食堂近"),
            ("A103 自习室", 6, 8, 2, 1, ""),
        ]),
        ("2F", [
            ("A201 自习室", 6, 8, 2, 1, "有空调"),
            ("A205 研讨室", 4, 6, 1, 0, "可讨论"),
        ]),
        ("3F", [
            ("A301 自习室", 6, 8, 3, 1, "静音区"),
            ("A303 自习室", 5, 8, 2, 0, "无插座"),
        ]),
    ]),
    ("第二教学楼", "B", "西区", [
        ("1F", [
            ("B101 自习室", 6, 8, 2, 0, "无插座"),
            ("B105 自习室", 5, 8, 2, 1, ""),
        ]),
        ("2F", [
            ("B201 通宵自习室", 7, 10, 2, 1, "24小时,插座多"),
        ]),
        ("4F", [
            ("B401 自习室", 6, 8, 3, 1, "静音区"),
            ("B403 自习室", 5, 8, 2, 1, "人少"),
        ]),
    ]),
]


def occupancy_rate(hour):
    """按一天中的时段给一个基础占用率，模拟真实潮汐。"""
    if 7 <= hour < 9:
        return 0.30     # 早读，座位很空
    if 9 <= hour < 12:
        return 0.62     # 上午课间，有人来
    if 12 <= hour < 14:
        return 0.45     # 午休回落
    if 14 <= hour < 17:
        return 0.70
    if 17 <= hour < 19:
        return 0.55     # 晚饭
    if 19 <= hour < 22:
        return 0.88     # 晚高峰，一座难求
    return 0.35


def build():
    db.init_db(force=True)
    conn = db.connect()
    # 按东八区取当前时段，否则线上服务器（UTC）算出来的"晚高峰"会错位 8 小时
    now = db.now_cn()
    base = occupancy_rate(now.hour)
    ts = now.strftime("%Y-%m-%d %H:%M:%S")

    try:
        total_seats = 0
        for b_name, b_code, campus, floors in LAYOUT:
            cur = conn.execute(
                "INSERT INTO building(name, code, campus) VALUES (?,?,?)",
                (b_name, b_code, campus),
            )
            b_id = cur.lastrowid

            for f_idx, (f_name, rooms) in enumerate(floors, start=1):
                cur = conn.execute(
                    "INSERT INTO floor(building_id, name, level) VALUES (?,?,?)",
                    (b_id, f_name, f_idx),
                )
                f_id = cur.lastrowid

                for r_name, rows, cols, quiet, socket, tags in rooms:
                    cur = conn.execute(
                        "INSERT INTO room(floor_id, name, grid_rows, grid_cols, quiet, has_socket, tags)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (f_id, r_name, rows, cols, quiet, socket, tags),
                    )
                    r_id = cur.lastrowid
                    total_seats += _fill_seats(conn, r_id, rows, cols, socket, base, ts)

        conn.commit()
        print(f"已生成 {total_seats} 个座位")
    finally:
        conn.close()


def _fill_seats(conn, room_id, rows, cols, has_socket, base, ts):
    """给一间自习室铺座位。中间留出过道，靠墙位置配插座。"""
    count = 0
    # 每 3 列后留一条过道，模拟真实排布
    aisle_cols = {3}
    for r in range(1, rows + 1):
        for c in range(1, cols + 1):
            if c in aisle_cols:
                continue
            code = f"{chr(64 + r)}{c:02d}"
            # 靠墙与过道边的座位有插座；整间没插座的房间就都没有
            socket = 1 if (has_socket and (c == 1 or c == cols or c == 4)) else 0
            occupied = random.random() < base
            # 有插座的座位更抢手，占用率再高一点
            if socket and random.random() < 0.12:
                occupied = True
            status = db.OCCUPIED if occupied else db.FREE
            source = db.SRC_ACCESS if random.random() < 0.6 else db.SRC_SOCKET
            conn.execute(
                "INSERT INTO seat(room_id, code, row_idx, col_idx, has_socket, status, source, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (room_id, code, r, c, socket, status, source, ts),
            )
            count += 1
    return count


def demo_reservations(user="同学A"):
    """造两条今天的预约，方便"我的预约"页面一打开就有内容。"""
    conn = db.connect()
    now = db.now_cn()
    try:
        seats = conn.execute(
            "SELECT id FROM seat WHERE status='free' ORDER BY RANDOM() LIMIT 2"
        ).fetchall()
        for i, s in enumerate(seats):
            start = now + timedelta(minutes=20 + i * 40)
            conn.execute(
                "INSERT INTO reservation(seat_id, user_name, start_at, end_at, remind_at,"
                " status, people, created_at) VALUES (?,?,?,?,?, 'active', ?, ?)",
                (
                    s["id"], user,
                    start.strftime("%Y-%m-%d %H:%M:%S"),
                    (start + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
                    (start - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S"),
                    1, now.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            conn.execute(
                "UPDATE seat SET status=?, source=? WHERE id=?",
                (db.RESERVED, db.SRC_SYSTEM, s["id"]),
            )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    build()
    demo_reservations()
    print(f"数据库位置：{db.DB_PATH}")
