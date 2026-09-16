import unittest

from app.officials import AssignmentService, Official


class AssignmentSmokeTest(unittest.TestCase):
    def test_official_and_health(self):
        self.assertEqual(Official("周宁", "国家二级").grade, "国家二级")
        self.assertEqual(AssignmentService().health()["status"], "ok")


if __name__ == "__main__":
    unittest.main()

