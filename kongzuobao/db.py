"""数据库层：建表、连接、初始化。

只用标准库 sqlite3，不引入额外 ORM，方便部署到腾讯云轻量服务器。
"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 强制判断 Vercel 环境
if os.environ.get("VERCEL") == "1":
    # Vercel 环境只有 /tmp 目录可以读写
    DB_PATH = "/tmp/kongzuobao.db"
else:
    # 本地环境（Windows/Mac）保持不变
    DB_PATH = os.environ.get("KONGZUOBAO_DB") or os.path.join(BASE_DIR, "kongzuobao.db")

# 为了方便在日志里排查，加一句打印
print(f"==== 当前数据库路径: {DB_PATH} ====")
CN_TZ = timezone(timedelta(hours=8))


def now_cn():
    """当前东八区时间，**不带 tzinfo**。

    库里存的是 'YYYY-MM-DD HH:MM:SS' 这种 naive 字符串，读出来 strptime 也是 naive。
    拿它做加减或比较时，基准时间也必须同样 naive —— 否则 aware 减 naive 会直接
    抛 "can't subtract offset-naive and offset-aware datetimes"。
    全项目统一走这个函数，就不会再踩混用的坑。
    """
    return datetime.now(CN_TZ).replace(tzinfo=None)

# 座位状态
FREE = "free"          # 空闲
OCCUPIED = "occupied"  # 已被占用
RESERVED = "reserved"  # 已被预约锁定

# 数据来源（文档里说的"融合门禁刷卡与插座用电数据、叠加众包上报"）
SRC_ACCESS = "access"    # 门禁刷卡
SRC_SOCKET = "socket"    # 插座用电
SRC_CROWD = "crowd"      # 众包上报
SRC_SYSTEM = "system"    # 系统初始化

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 楼栋
CREATE TABLE IF NOT EXISTS building (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    code     TEXT NOT NULL,
    campus   TEXT NOT NULL,          -- 校区/区域，如"东区"
    open_hour TEXT NOT NULL DEFAULT '07:00',
    close_hour TEXT NOT NULL DEFAULT '22:30'
);

-- 楼层
CREATE TABLE IF NOT EXISTS floor (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    building_id INTEGER NOT NULL REFERENCES building(id),
    name        TEXT NOT NULL,       -- 1F / 2F ...
    level       INTEGER NOT NULL
);

-- 自习室 / 阅览室
CREATE TABLE IF NOT EXISTS room (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    floor_id    INTEGER NOT NULL REFERENCES floor(id),
    name        TEXT NOT NULL,
    grid_rows   INTEGER NOT NULL DEFAULT 6,   -- 座位图行数
    grid_cols   INTEGER NOT NULL DEFAULT 8,   -- 座位图列数
    quiet       INTEGER NOT NULL DEFAULT 2,   -- 安静程度 1=可讨论 2=较安静 3=静音区
    has_socket  INTEGER NOT NULL DEFAULT 1,   -- 是否配备插座
    tags        TEXT NOT NULL DEFAULT ''      -- 逗号分隔标签
);

-- 座位
CREATE TABLE IF NOT EXISTS seat (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id    INTEGER NOT NULL REFERENCES room(id),
    code       TEXT NOT NULL,        -- A01
    row_idx    INTEGER NOT NULL,
    col_idx    INTEGER NOT NULL,
    has_socket INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'free',
    source     TEXT NOT NULL DEFAULT 'system',  -- 该状态由哪路数据得出
    updated_at TEXT NOT NULL
);

-- 预约
CREATE TABLE IF NOT EXISTS reservation (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    seat_id    INTEGER NOT NULL REFERENCES seat(id),
    user_name  TEXT NOT NULL,
    start_at   TEXT NOT NULL,
    end_at     TEXT NOT NULL,
    remind_at  TEXT NOT NULL,
    reminded   INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'active',  -- active/cancelled/finished
    people     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- 众包上报
CREATE TABLE IF NOT EXISTS report (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    seat_id    INTEGER NOT NULL REFERENCES seat(id),
    user_name  TEXT NOT NULL,
    claim      TEXT NOT NULL,        -- free | occupied
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- 会员（对应文档里的 9.9 元/月）
CREATE TABLE IF NOT EXISTS member (
    user_name  TEXT PRIMARY KEY,
    plan       TEXT NOT NULL DEFAULT 'free',
    expire_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_seat_room ON seat(room_id);
CREATE INDEX IF NOT EXISTS idx_floor_building ON floor(building_id);
CREATE INDEX IF NOT EXISTS idx_res_seat ON reservation(seat_id);
"""


def connect():
    # timeout / busy_timeout：并发写时排队等待，而不是立刻抛 database is locked
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL：模拟线程在后台持续写状态时，前台的读请求不用等锁。
    # 线上多进程访问同一个库时这条尤其关键。
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(force=False):
    """建表。force=True 时删除旧库重建（配合 seed 使用）。"""
    if force and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return DB_PATH


def is_empty():
    """库里还没有楼栋数据，说明没初始化过。"""
    conn = connect()
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM building").fetchone()
        return row["c"] == 0
    finally:
        conn.close()


if __name__ == "__main__":
    print(init_db())
