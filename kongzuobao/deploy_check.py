"""部署前自检：用一份临时数据库把全部接口跑一遍。

顺带验证三件线上才会暴露的事：KONGZUOBAO_DB 能否改库路径、时间是否走东八区、
WAL 是否生效。用完即可删除，不属于项目本身。
"""
import os
import shutil
import sys
import tempfile

# ⚠️ 必须在 import app 之前设好：app 导入时就会建库、灌数据、起模拟线程。
TMP_DIR = tempfile.mkdtemp(prefix="kzb_check_")
os.environ["KONGZUOBAO_DB"] = os.path.join(TMP_DIR, "check.db")

import datetime  # noqa: E402

import app as appmod  # noqa: E402
import db  # noqa: E402

c = appmod.app.test_client()
failures = []


def check(name, cond, detail=""):
    if not cond:
        failures.append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


try:
    # ---------- 数据库设置 ----------
    check("KONGZUOBAO_DB 能改库路径", db.DB_PATH == os.environ["KONGZUOBAO_DB"], db.DB_PATH)
    conn = db.connect()
    check("SQLite 已开 WAL",
          conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal")
    check("busy_timeout 已设", conn.execute("PRAGMA busy_timeout").fetchone()[0] == 15000)
    conn.close()

    # ---------- 时区 ----------
    cn_now = db.now_cn()
    check("now_cn() 不带 tzinfo（能和库里的 naive 字符串直接相减）",
          cn_now.tzinfo is None)
    check("now_cn() 与 UTC 差 8 小时",
          abs((datetime.datetime.utcnow() - cn_now).total_seconds() + 8 * 3600) < 60,
          f"now_cn={fmt(cn_now)}")

    # ---------- 首屏与筛选 ----------
    r = c.get("/api/overview")
    ov = r.get_json()
    check("GET /api/overview", r.status_code == 200)
    check("server_time 是东八区", ov["server_time"][:16] == fmt(cn_now)[:16],
          f"接口={ov['server_time']}")
    check("首屏有座位总数与空位数", ov["total"] > 0, f"{ov['free']}/{ov['total']}")
    check("推荐位非空", len(ov["recommend"]) > 0)

    meta = c.get("/api/meta").get_json()
    check("GET /api/meta 返回 3 栋楼", len(meta["buildings"]) == 3, f"{len(meta['buildings'])} 栋")

    rooms = c.get("/api/rooms?socket=1&quiet=3&people=2").get_json()
    check("GET /api/rooms 组合筛选有结果", rooms["count"] > 0, f"{rooms['count']} 间符合")
    check("筛选结果空位数都 >= 同行人数",
          all(x["stats"]["free"] >= 2 for x in rooms["rooms"]))

    room_id = rooms["rooms"][0]["id"]
    detail = c.get(f"/api/rooms/{room_id}").get_json()
    check(f"GET /api/rooms/{room_id} 座位图", len(detail["seats"]) > 0,
          f"{len(detail['seats'])} 个座位")
    check("座位带数据来源标注",
          detail["seats"][0]["source"] in ("门禁刷卡", "插座用电", "众包上报", "系统"),
          detail["seats"][0]["source"])
    check("GET 不存在的自习室返回 404", c.get("/api/rooms/999999").status_code == 404)

    # ---------- 预约 → 提醒 → 冲突 → 取消 ----------
    conn = db.connect()
    seat_id = conn.execute("SELECT id FROM seat WHERE status='free' LIMIT 1").fetchone()["id"]
    conn.close()

    res = c.post("/api/reserve", json={"seat_id": seat_id, "user": "部署自检", "minutes": 120})
    check("POST /api/reserve", res.status_code == 200 and res.get_json().get("ok"),
          res.get_json().get("msg"))

    mine = c.get("/api/reservations?user=部署自检").get_json()
    check("GET /api/reservations 只返回自己的预约", len(mine["reservations"]) == 1,
          f"{len(mine['reservations'])} 条")
    if mine["reservations"]:
        item = mine["reservations"][0]
        drift = abs((db.now_cn() - datetime.datetime.strptime(item["start_at"], "%Y-%m-%d %H:%M:%S")
                     ).total_seconds())
        check("预约开始时间 = 现在（东八区，没偏 8 小时）", drift < 120, f"偏差 {drift:.0f} 秒")
        check("剩余分钟数合理", 0 <= item["remain_minutes"] <= 121,
              f"{item['remain_minutes']} 分钟")
        check("立即开始的预约会立刻触发到点提醒", len(mine["reminders"]) == 1,
              f"{len(mine['reminders'])} 条")

        r = c.post("/api/reserve", json={"seat_id": seat_id, "user": "同学B"})
        check("别人抢已预约的座位被拒（409）", r.status_code == 409, r.get_json().get("msg"))

        r = c.post(f"/api/reservations/{item['id']}/cancel")
        check("POST 取消预约", r.status_code == 200 and r.get_json().get("ok"))
        conn = db.connect()
        st = conn.execute("SELECT status FROM seat WHERE id=?", (item["seat_id"],)).fetchone()["status"]
        conn.close()
        check("取消后座位立即释放", st == "free", f"status={st}")

    # ---------- 众包上报 ----------
    r = c.post("/api/report", json={"seat_id": seat_id, "user": "热心同学", "claim": "occupied"})
    check("POST /api/report", r.status_code == 200 and r.get_json().get("ok"))
    conn = db.connect()
    row = conn.execute("SELECT status, source FROM seat WHERE id=?", (seat_id,)).fetchone()
    conn.close()
    check("上报后座位状态与来源同步修正",
          row["status"] == "occupied" and row["source"] == db.SRC_CROWD,
          f"{row['status']} / {row['source']}")
    check("GET /api/reports", c.get("/api/reports").status_code == 200)

    r = c.post("/api/reserve", json={"seat_id": seat_id, "user": "普通同学"})
    check("非会员抢已占座位被拒（409）", r.status_code == 409, r.get_json().get("msg"))

    # ---------- 会员 ----------
    check("GET /api/member 默认非会员",
          c.get("/api/member?user=部署自检").get_json()["is_member"] is False)
    c.post("/api/member", json={"user": "部署自检", "plan": "pro"})
    check("POST 开通会员后 is_member=True",
          c.get("/api/member?user=部署自检").get_json()["is_member"] is True)
    m = c.get("/api/member?user=部署自检").get_json()
    check("会员价格仍是 9.9", m["price"] == 9.9)

    # ---------- 模拟线程 ----------
    t = appmod.start_simulator()
    check("RUN_SIMULATOR=1 时模拟线程在跑", t is not None and t.is_alive())
finally:
    shutil.rmtree(TMP_DIR, ignore_errors=True)

print()
if failures:
    print(f"自检未通过，共 {len(failures)} 项：")
    for f in failures:
        print("  - " + f)
else:
    print("自检全部通过。")
sys.exit(1 if failures else 0)
