"""端到端演示：报名截止后发现利益冲突 → 重新评估 → 替补接管 → 导出。

运行：python3 examples/demo.py
"""

import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.service import AssignmentService
from app.testing import FakeClock

DIRECTOR = "竞赛主任"


def main() -> None:
    clock = FakeClock(datetime(2026, 9, 18, 8, 0, tzinfo=timezone.utc))
    service = AssignmentService(clock=clock)

    # 1. 登记裁判档案：等级 / 可执法项目 / 执法区域 / 常驻城市。
    service.add_referee(
        referee_id="R1", name="王强", level="NATIONAL",
        events={"5v5"}, regions={"华东"}, home_city="上海",
    )
    service.add_referee(
        referee_id="R2", name="李敏", level="NATIONAL",
        events={"5v5"}, regions={"华东"}, home_city="上海",
    )
    service.add_referee(
        referee_id="R3", name="张伟", level="LEVEL_1",
        events={"5v5"}, regions={"华东"}, home_city="杭州",
    )
    # 王强每晚 22:00 到次日 02:00（跨天）不可用。
    service.add_daily_unavailability(
        "R1", "22:00", "02:00", tz="Asia/Shanghai",
        days=[date(2026, 10, 1)], reason="夜间休息",
    )

    # 2. 登记比赛。
    match = service.add_match(
        match_id="M1", name="半决赛：蓝鲸 vs 猛虎", level="SEMIFINAL",
        event="5v5", region="华东", city="上海", tz="Asia/Shanghai",
        start=datetime(2026, 10, 1, 19, 0), end=datetime(2026, 10, 1, 21, 0),
        teams=("蓝鲸队", "猛虎队"),
    )

    # 3. 生成候选名单（按级别/距离/连续工作时长/近期场次打分）。
    report = service.generate_candidates(match.id)
    print("== 候选名单 ==")
    for c in report.candidates:
        print(f"  {c.referee_name}  score={c.score}  距离={c.distance_km}km")
    for e in report.excluded:
        print(f"  [排除] {e.referee_name} {e.code}: {e.detail}")

    # 4. 主任锁定并确认，裁判带理由接受。
    token = service.lock_candidates(match.id, ["R1"], director=DIRECTOR)["lock_token"]
    assignment = service.confirm(match.id, expected_version=token, director=DIRECTOR)[0]
    service.respond(assignment.id, referee_id="R1", accept=True, reason="档期合适")

    # 5. 报名截止后发现利益冲突：王强申报与蓝鲸队的培训关系。
    result = service.declare_conflict("R1", "蓝鲸队", "TRAINING", "曾任该队训练营讲师")
    print("\n== 冲突重新评估 ==")
    for item in result["reevaluated"]:
        print(f"  {item['action']}: {item['explanation']}")

    # 6. 冲突解释接口。
    explanation = service.explain(match.id, "R1")
    print(f"\n== 冲突解释 ==\n  {explanation['summary']}")

    # 7. 替补接管。
    takeover = service.substitute(assignment.id, director=DIRECTOR, reason="冲突回避")
    new = takeover["assignment"]
    print(f"\n== 替补接管 ==\n  {new.referee_id} 接替 {takeover['replaced_referee_id']}")

    # 8. 时间线可追溯。
    print("\n== 原指派时间线 ==")
    for event in service.timeline(assignment.id):
        print(f"  #{event.seq} {event.at:%H:%M} {event.actor}: {event.action} ({event.reason})")

    # 9. 按日期导出。
    exported = service.export_by_date(date(2026, 10, 1), "Asia/Shanghai")
    print("\n== 2026-10-01 导出 ==")
    for row in exported["rows"]:
        print(f"  {row['match_name']}  裁判={row['referee_name'] or '—'}  状态={row['assignment_status']}")


if __name__ == "__main__":
    main()
