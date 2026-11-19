"""冒烟测试：把「找座 → 看座位图 → 预约 → 提醒 → 上报 → 取消」整条链路跑一遍。

用法（先启动 app.py）：
    python smoke_test.py
"""
import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:5001"
out = io.StringIO()


def call(path, method="GET", payload=None):
    """发请求并解析 JSON。4xx 也当正常响应返回 body，方便断言业务层的拒绝。"""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + urllib.parse.quote(path, safe="/?=&"), data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8"))


def log(*a):
    print(*a)
    print(*a, file=out)


def main():
    ok, fail = 0, 0

    def check(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            log(f"  [PASS] {name} {extra}")
        else:
            fail += 1
            log(f"  [FAIL] {name} {extra}")

    log("=" * 62)
    log("空座宝 · 接口冒烟测试")
    log("=" * 62)

    # 1. 楼栋楼层
    meta = call("/api/meta")
    log(f"\n[1] 楼栋/楼层：{len(meta['buildings'])} 栋")
    for b in meta["buildings"]:
        log(f"    {b['name']:8s} 空位 {b['free']:3d}/{b['total']:3d}"
            f"  楼层 {'/'.join(f['name'] for f in b['floors'])}")
    check("至少 3 栋楼", len(meta["buildings"]) >= 3)
    check("楼层已展开", all(b["floors"] for b in meta["buildings"]))

    # 2. 首屏推荐
    ov = call("/api/overview")
    log(f"\n[2] 首屏：全校空位 {ov['free']}/{ov['total']}，空位率 {ov['free_ratio']:.0%}")
    for r in ov["recommend"][:3]:
        log(f"    推荐 {r['name']:16s} {r['building']}{r['floor']}"
            f"  空位 {r['stats']['free']:3d}  {r['risk_tip']}")
    check("返回推荐列表", len(ov["recommend"]) > 0)
    check("推荐已排序", ov["recommend"][0]["stats"]["free_ratio"]
          >= ov["recommend"][-1]["stats"]["free_ratio"])

    # 3. 筛选：有插座 + 静音区 + 2 人
    flt = call("/api/rooms?socket=1&quiet=3&people=2")
    log(f"\n[3] 筛选（有插座+静音区+2人）：{flt['count']} 间")
    for r in flt["rooms"][:3]:
        log(f"    {r['name']:16s} 空位 {r['stats']['free']:3d}  {r['quiet_label']}")
    check("筛选生效", all(r["has_socket"] and r["quiet"] >= 3
                          and r["stats"]["free"] >= 2 for r in flt["rooms"]))

    # 4. 座位图
    room_id = ov["recommend"][0]["id"]
    detail = call(f"/api/rooms/{room_id}")
    seats = detail["seats"]
    log(f"\n[4] 座位图：{detail['room']['name']} 共 {len(seats)} 座，"
        f"空 {detail['room']['stats']['free']} / 约 {detail['room']['stats']['reserved']}"
        f" / 占 {detail['room']['stats']['occupied']}")
    check("座位数匹配", len(seats) == detail["room"]["stats"]["total"])
    check("座位有坐标", all(s["row"] and s["col"] for s in seats))
    check("状态取值合法", all(s["status"] in ("free", "occupied", "reserved") for s in seats))

    # 5. 预约
    free_seat = next((s for s in seats if s["status"] == "free"), None)
    check("存在空座可预约", free_seat is not None)
    if free_seat:
        res = call("/api/reserve", "POST", {"seat_id": free_seat["id"], "user": "测试同学", "minutes": 120})
        log(f"\n[5] 预约 {free_seat['code']}：{res['msg']}")
        check("预约成功", res.get("ok") is True)

        again = call("/api/reserve", "POST", {"seat_id": free_seat["id"], "user": "另一个人"})
        log(f"    重复预约同一座位：{again['msg']}")
        check("重复预约被拒绝", again.get("ok") is False)

        after = call(f"/api/rooms/{room_id}")
        st = next(s for s in after["seats"] if s["id"] == free_seat["id"])
        check("座位转为已预约", st["status"] == "reserved", f"(source={st['source']})")

        # 6. 我的预约 + 提醒
        mine = call("/api/reservations?user=测试同学")
        log(f"\n[6] 我的预约：{len(mine['reservations'])} 条，"
            f"待提醒 {len(mine['reminders'])} 条")
        if mine["reservations"]:
            r0 = mine["reservations"][0]
            log(f"    {r0['building']} {r0['floor']} {r0['room']} {r0['seat_code']}"
                f"  剩余 {r0['remain_minutes']} 分钟")
        check("预约已入库", len(mine["reservations"]) == 1)
        check("到点提醒已触发", len(mine["reminders"]) == 1)

        # 7. 众包上报
        rep = call("/api/report", "POST",
                   {"seat_id": seats[1]["id"], "user": "热心同学", "claim": "occupied"})
        log(f"\n[7] 众包上报：{rep['msg']}")
        check("上报成功", rep.get("ok") is True)
        reports = call("/api/reports")
        check("上报已记录", len(reports["reports"]) >= 1)

        # 8. 取消预约
        cid = mine["reservations"][0]["id"]
        can = call(f"/api/reservations/{cid}/cancel", "POST")
        log(f"\n[8] 取消预约：{can['msg']}")
        check("取消成功", can.get("ok") is True)
        final = call(f"/api/rooms/{room_id}")
        st2 = next(s for s in final["seats"] if s["id"] == free_seat["id"])
        check("座位已释放", st2["status"] in ("free", "occupied"), f"(status={st2['status']})")

    # 9. 会员
    m = call("/api/member?user=我")
    log(f"\n[9] 会员：{m['is_member']}，价格 ¥{m['price']}，权益 {len(m['benefits'])} 项")
    check("会员接口正常", m["price"] == 9.9)
    up = call("/api/member", "POST", {"user": "我", "plan": "pro"})
    check("可开通会员", up.get("is_member") is True)
    call("/api/member", "POST", {"user": "我", "plan": "free"})

    log("\n" + "=" * 62)
    log(f"结果：{ok} 项通过，{fail} 项失败")
    log("=" * 62)

    with io.open("smoke_report.txt", "w", encoding="utf-8") as f:
        f.write(out.getvalue())
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
