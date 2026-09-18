"""测试共享构造工具。"""

from __future__ import annotations

from app.service import AssignmentService
from app.timeutils import local_datetime


def build_league(policy=None) -> AssignmentService:
    """构造一个典型联赛：

    队伍：上海闪电 t_sh、杭州雷霆 t_hz、广州南狮 t_gz
    裁判：
      r_zhou 国家级  常驻上海，区域含杭州（全能候选）
      r_wu   国家一级 常驻杭州，区域含上海
      r_low  国家二级 常驻上海（甲级不够格）
      r_ballonly 国家级 常驻上海，但仅能执篮球以外的另类项目不可用于篮球
    """
    svc = AssignmentService(policy=policy)
    svc.create_team("t_sh", "上海闪电", "上海")
    svc.create_team("t_hz", "杭州雷霆", "杭州")
    svc.create_team("t_gz", "广州南狮", "广州")

    svc.create_official(
        "r_zhou", "周宁", "国家级", "上海", region_cities={"杭州", "广州"}
    )
    svc.create_official(
        "r_wu", "吴敏", "国家一级", "杭州", region_cities={"上海"}
    )
    svc.create_official("r_low", "郑海", "国家二级", "上海")
    svc.create_official(
        "r_onlyvolley", "替补排球裁判", "国家级", "上海", sports={"排球"}
    )
    return svc


def make_match(
    svc: AssignmentService,
    match_id: str = "m1",
    level: str = "甲级",
    city: str = "上海",
    start="2026-09-20T19:00",
    end="2026-09-20T21:00",
    home="t_sh",
    away="t_hz",
    tz="Asia/Shanghai",
    sport="篮球",
    venue="源深体育馆",
):
    return svc.create_match(
        match_id, level, sport, home, away, city, venue, start, end, tz
    )
