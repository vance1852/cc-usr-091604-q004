"""基础冒烟：领域对象与服务健康检查（适配新的领域模型）。"""

import unittest

from app.models import Grade, Official
from app.service import AssignmentService


class AssignmentSmokeTest(unittest.TestCase):
    def test_official_and_health(self):
        official = Official(
            id="r1", name="周宁", grade=Grade.LEVEL_2, home_city="上海"
        )
        self.assertEqual(official.grade, Grade("国家二级"))
        self.assertTrue(official.can_ref_sport("篮球"))
        self.assertEqual(AssignmentService().health()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
