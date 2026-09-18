"""HTTP（WSGI）接口测试：端到端 JSON 契约与错误码区分。"""

import io
import json
import unittest

from app.api import WSGIApp
from app.service import AssignmentService

from tests.fixtures import build_league


class WSGIClient:
    def __init__(self, app: WSGIApp):
        self.app = app

    def request(self, method: str, path: str, body: dict | None = None):
        if "?" in path:
            path, query_string = path.split("?", 1)
        else:
            query_string = ""
        payload = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query_string,
            "CONTENT_LENGTH": str(len(payload)),
            "CONTENT_TYPE": "application/json",
            "wsgi.input": io.BytesIO(payload),
        }
        captured: dict = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = headers

        raw = b"".join(self.app(environ, start_response))
        code = int(captured["status"].split(" ", 1)[0])
        return code, json.loads(raw.decode("utf-8"))


class ApiTest(unittest.TestCase):
    def setUp(self):
        # 用既有联赛夹具填充一个 service，再包成 WSGI app
        self.svc = build_league()
        self.app = WSGIApp(self.svc)
        self.client = WSGIClient(self.app)
        # 建一场默认比赛
        code, _ = self.client.request(
            "POST", "/api/matches",
            {
                "id": "m1", "level": "甲级", "sport": "篮球",
                "home_team_id": "t_sh", "away_team_id": "t_hz",
                "city": "上海", "venue": "源深体育馆",
                "start": "2026-09-20T19:00", "end": "2026-09-20T21:00",
            },
        )
        self.assertEqual(code, 201)

    def test_health(self):
        code, body = self.client.request("GET", "/api/health")
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "ok")

    def test_candidates_distinguish_excluded_and_disqualified(self):
        code, body = self.client.request("GET", "/api/matches/m1/candidates")
        self.assertEqual(code, 200)
        self.assertEqual(body["summary"]["outcome"], "candidates_available")
        # 硬性不合格：r_low 等级不足
        dq = {row["official_id"]: row for row in body["disqualified"]}
        self.assertIn("r_low", dq)
        self.assertTrue(any(r["code"] == "grade_too_low" for r in dq["r_low"]["reasons"]))

    def test_candidates_all_excluded_outcome(self):
        # 给所有硬资格合格者都设置障碍：周宁利益冲突、吴敏不可用
        self.client.request(
            "POST", "/api/officials/r_zhou/conflicts",
            {"team_id": "t_sh", "conflict_type": "family", "detail": "亲属在队"},
        )
        self.client.request(
            "POST", "/api/officials/r_wu/unavailability",
            {"start": "2026-09-20T18:00", "end": "2026-09-20T22:00", "reason": "出差"},
        )
        code, body = self.client.request("GET", "/api/matches/m1/candidates")
        self.assertEqual(code, 200)
        self.assertEqual(body["summary"]["outcome"], "all_excluded_by_rules")
        codes = {r["code"] for row in body["excluded"] for r in row["reasons"]}
        self.assertIn("conflict", codes)
        self.assertIn("unavailable", codes)

    def test_lock_confirm_respond_and_timeline(self):
        code, locked = self.client.request(
            "POST", "/api/assignments/lock",
            {"match_id": "m1", "official_id": "r_zhou", "actor": "主任李"},
        )
        self.assertEqual(code, 201)
        aid = locked["id"]
        self.assertEqual(locked["status"], "locked")

        code, confirmed = self.client.request(
            "POST", f"/api/assignments/{aid}/confirm",
            {"actor": "主任李", "expected_version": 1},
        )
        self.assertEqual(code, 200)
        self.assertEqual(confirmed["status"], "confirmed")

        code, accepted = self.client.request(
            "POST", f"/api/assignments/{aid}/respond",
            {"accept": True, "reason": "准时到场", "actor": "r_zhou"},
        )
        self.assertEqual(code, 200)
        self.assertEqual(accepted["status"], "accepted")

        code, timeline = self.client.request(
            "GET", f"/api/assignments/{aid}/timeline"
        )
        self.assertEqual(
            [e["action"] for e in timeline["timeline"]],
            ["locked", "confirmed", "accepted"],
        )

    def test_confirm_stale_version_returns_conflict(self):
        _, locked = self.client.request(
            "POST", "/api/assignments/lock",
            {"match_id": "m1", "official_id": "r_zhou", "actor": "主任李"},
        )
        aid = locked["id"]
        # 先用正确版本确认成功
        code, _ = self.client.request(
            "POST", f"/api/assignments/{aid}/confirm",
            {"actor": "主任李", "expected_version": 1},
        )
        self.assertEqual(code, 200)
        # 再用陈旧版本重复确认
        code, err = self.client.request(
            "POST", f"/api/assignments/{aid}/confirm",
            {"actor": "主任甲", "expected_version": 1},
        )
        self.assertEqual(code, 409)
        self.assertEqual(err["error_code"], "state_conflict")

    def test_lock_conflicted_returns_409_with_exclusion_reasons(self):
        self.client.request(
            "POST", "/api/officials/r_zhou/conflicts",
            {"team_id": "t_sh", "conflict_type": "training"},
        )
        code, err = self.client.request(
            "POST", "/api/assignments/lock",
            {"match_id": "m1", "official_id": "r_zhou", "actor": "主任李"},
        )
        self.assertEqual(code, 409)
        self.assertEqual(err["error_code"], "excluded_by_rules")
        self.assertTrue(any(r["code"] == "conflict" for r in err["reasons"]))

    def test_batch_and_export(self):
        # 再建一场用于批量
        self.client.request(
            "POST", "/api/matches",
            {
                "id": "m2", "level": "甲级", "sport": "篮球",
                "home_team_id": "t_sh", "away_team_id": "t_hz",
                "city": "杭州", "venue": "杭体",
                "start": "2026-09-21T19:00", "end": "2026-09-21T21:00",
            },
        )
        code, body = self.client.request(
            "POST", "/api/assignments/batch",
            {
                "actor": "主任李",
                "requests": [
                    {"match_id": "m1", "official_id": "r_zhou"},
                    {"match_id": "m2", "official_id": "r_wu"},
                ],
            },
        )
        self.assertEqual(code, 200)
        self.assertTrue(body["committed"])

        code, exported = self.client.request(
            "GET", "/api/export?date=2026-09-20&tz=Asia/Shanghai"
        )
        self.assertEqual(code, 200)
        ids = [row["match"]["id"] for row in exported["matches"]]
        self.assertEqual(ids, ["m1"])
        self.assertEqual(len(exported["matches"][0]["assignments"]), 1)

    def test_takeover_endpoint(self):
        _, locked = self.client.request(
            "POST", "/api/assignments/lock",
            {"match_id": "m1", "official_id": "r_zhou", "actor": "主任李"},
        )
        aid = locked["id"]
        self.client.request("POST", f"/api/assignments/{aid}/confirm", {"actor": "主任李"})
        self.client.request(
            "POST", f"/api/assignments/{aid}/respond",
            {"accept": False, "reason": "受伤", "actor": "r_zhou"},
        )
        code, body = self.client.request(
            "POST", "/api/matches/m1/takeover",
            {"new_official_id": "r_wu", "reason": "原裁判受伤", "actor": "主任李"},
        )
        self.assertEqual(code, 201)
        self.assertEqual(body["old_assignment_id"], aid)
        self.assertEqual(body["new_official_id"], "r_wu")

    def test_finished_match_is_protected_over_http(self):
        _, locked = self.client.request(
            "POST", "/api/assignments/lock",
            {"match_id": "m1", "official_id": "r_zhou", "actor": "主任李"},
        )
        aid = locked["id"]
        self.client.request("POST", "/api/matches/m1/start", {})
        self.client.request("POST", "/api/matches/m1/finish", {})
        code, err = self.client.request(
            "POST", f"/api/assignments/{aid}/cancel",
            {"reason": "事后改派", "actor": "主任李"},
        )
        self.assertEqual(code, 409)
        self.assertEqual(err["error_code"], "historical_protected")

    def test_not_found_match(self):
        code, err = self.client.request("GET", "/api/matches/nope/candidates")
        self.assertEqual(code, 404)
        self.assertEqual(err["error_code"], "not_found")


if __name__ == "__main__":
    unittest.main()
