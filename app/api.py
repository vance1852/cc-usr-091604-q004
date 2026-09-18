"""WSGI HTTP 接口（仅依赖标准库 wsgiref）。

响应中错误码区分：
- ``not_qualified``：硬性资格不满足（未找到合格人选侧）；
- ``excluded_by_rules``：资格合格但被利益冲突/不可用/排班规则排除；
- ``state_conflict``：并发版本冲突或非法状态流转；
- ``historical_protected``：已结束比赛的历史指派受保护。
"""

from __future__ import annotations



import json
import re
from datetime import date
from http import HTTPStatus
from urllib.parse import parse_qs, urlparse
from wsgiref.simple_server import make_server

from .service import (
    AssignmentError,
    AssignmentService,
    EligibilityError,
    HistoricalAssignmentProtected,
    NotFoundError,
    RuleExclusionError,
    StateConflictError,
)


def _error_body(exc: AssignmentError) -> dict:
    return {
        "error_code": exc.code,
        "message": str(exc),
        "reasons": [r.to_dict() for r in getattr(exc, "reasons", [])],
    }


def _status_for(exc: AssignmentError) -> int:
    if isinstance(exc, NotFoundError):
        return HTTPStatus.NOT_FOUND
    if isinstance(
        exc,
        (
            EligibilityError,
            RuleExclusionError,
            StateConflictError,
            HistoricalAssignmentProtected,
        ),
    ):
        return HTTPStatus.CONFLICT
    return HTTPStatus.BAD_REQUEST


class WSGIApp:
    def __init__(self, service: AssignmentService | None = None) -> None:
        self.service = service or AssignmentService()

    def __call__(self, environ, start_response):
        method = environ["REQUEST_METHOD"]
        path = urlparse(environ["PATH_INFO"]).path
        query = parse_qs(environ["QUERY_STRING"])
        try:
            body = self._read_json(environ)
            status, payload = self.route(method, path, query, body)
        except AssignmentError as exc:
            status, payload = _status_for(exc), _error_body(exc)
        except (ValueError, KeyError, TypeError) as exc:
            status = HTTPStatus.BAD_REQUEST
            payload = {"error_code": "bad_request", "message": str(exc), "reasons": []}

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        start_response(
            f"{status} {HTTPStatus(status).phrase}",
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(data))),
            ],
        )
        return [data]

    @staticmethod
    def _read_json(environ) -> dict:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if length == 0:
            return {}
        raw = environ["wsgi.input"].read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def route(self, method, path, query, body) -> tuple[int, dict]:
        svc = self.service

        if method == "GET" and path == "/api/health":
            return 200, svc.health()

        if method == "POST" and path == "/api/officials":
            o = svc.create_official(
                body["id"],
                body["name"],
                body["grade"],
                body["home_city"],
                sports=set(body.get("sports", ["篮球"])),
                region_cities=set(body.get("region_cities", [])),
            )
            return 201, {"id": o.id, "name": o.name, "grade": o.grade.value}

        if method == "POST" and path == "/api/teams":
            t = svc.create_team(body["id"], body["name"], body["city"])
            return 201, {"id": t.id, "name": t.name, "city": t.city}

        if method == "POST" and path == "/api/matches":
            m = svc.create_match(
                body["id"],
                body["level"],
                body.get("sport", "篮球"),
                body["home_team_id"],
                body["away_team_id"],
                body["city"],
                body["venue"],
                body["start"],
                body["end"],
                body.get("tz", "Asia/Shanghai"),
            )
            return 201, {"id": m.id, "start": m.start.isoformat(), "end": m.end.isoformat()}

        m_official = re.fullmatch(r"/api/officials/(?P<oid>[^/]+)/(?P<res>unavailability|conflicts)", path)
        if method == "POST" and m_official:
            oid = m_official.group("oid")
            if m_official.group("res") == "unavailability":
                result = svc.add_unavailability(
                    oid,
                    body["start"],
                    body["end"],
                    body["reason"],
                    body.get("tz", "Asia/Shanghai"),
                )
            else:
                result = svc.declare_conflict(
                    oid, body["team_id"], body["conflict_type"], body.get("detail", "")
                )
            return 201, result

        m_candidates = re.fullmatch(r"/api/matches/(?P<mid>[^/]+)/candidates", path)
        if method == "GET" and m_candidates:
            limit_raw = query.get("limit", [None])[0]
            limit = int(limit_raw) if limit_raw else None
            return 200, svc.candidates(m_candidates.group("mid"), limit=limit)

        m_explain = re.fullmatch(r"/api/matches/(?P<mid>[^/]+)/explain", path)
        if method == "GET" and m_explain:
            oid = query.get("official_id", [None])[0]
            if not oid:
                raise ValueError("explain 需要 official_id 查询参数")
            return 200, svc.explain(m_explain.group("mid"), oid)

        m_lifecycle = re.fullmatch(r"/api/matches/(?P<mid>[^/]+)/(?P<act>start|finish)", path)
        if method == "POST" and m_lifecycle:
            mid = m_lifecycle.group("mid")
            if m_lifecycle.group("act") == "start":
                svc.mark_started(mid)
            else:
                svc.mark_finished(mid)
            return 200, {"match_id": mid, "action": m_lifecycle.group("act")}

        if method == "POST" and path == "/api/assignments/lock":
            a = svc.lock_candidate(body["match_id"], body["official_id"], body.get("actor", "director"))
            return 201, a.to_dict()

        if method == "POST" and path == "/api/assignments/batch":
            return 200, svc.batch_assign(body["requests"], body.get("actor", "director"))

        m_takeover = re.fullmatch(r"/api/matches/(?P<mid>[^/]+)/takeover", path)
        if method == "POST" and m_takeover:
            return 201, svc.substitute_takeover(
                m_takeover.group("mid"),
                body["new_official_id"],
                body["reason"],
                body.get("actor", "director"),
                body.get("old_assignment_id"),
            )

        m_asn = re.fullmatch(r"/api/assignments/(?P<aid>[^/]+)/(?P<act>confirm|respond|cancel)", path)
        if method == "POST" and m_asn:
            aid = m_asn.group("aid")
            act = m_asn.group("act")
            if act == "confirm":
                a = svc.confirm_assignment(
                    aid, body.get("actor", "director"), body.get("expected_version")
                )
            elif act == "respond":
                a = svc.respond(
                    aid, bool(body["accept"]), body["reason"], body.get("actor")
                )
            else:
                a = svc.cancel_assignment(aid, body["reason"], body.get("actor", "director"))
            return 200, a.to_dict()

        m_timeline = re.fullmatch(r"/api/assignments/(?P<aid>[^/]+)/timeline", path)
        if method == "GET" and m_timeline:
            return 200, svc.assignment_timeline(m_timeline.group("aid"))

        if method == "GET" and path == "/api/export":
            day_raw = query.get("date", [None])[0]
            if not day_raw:
                raise ValueError("export 需要 date=YYYY-MM-DD 查询参数")
            tz_name = query.get("tz", ["Asia/Shanghai"])[0]
            return 200, svc.export_by_date(date.fromisoformat(day_raw), tz_name)

        return HTTPStatus.NOT_FOUND, {"error_code": "not_found", "message": f"无此路由：{method} {path}", "reasons": []}


def main(host: str = "127.0.0.1", port: int = 8000) -> None:
    server = make_server(host, port, WSGIApp())
    print(f"裁判指派服务已启动：http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
