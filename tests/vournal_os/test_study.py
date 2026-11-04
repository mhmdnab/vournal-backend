"""Course grades (study.util): weighted when any graded assessment has a weight, else the
plain average; the overall average weights by credits only when every course has them."""

import unittest
from types import SimpleNamespace as A


class Grades(unittest.TestCase):
    def test_weighted_plain_and_ungraded(self):
        from app.os.study import compute_grade

        weighted = compute_grade([A(grade=78, maxGrade=90, weight=30), A(grade=9.5, maxGrade=10, weight=10),
                                  A(grade=100, maxGrade=None, weight=None), A(grade=None, maxGrade=None, weight=60)])
        self.assertEqual(weighted, {"currentGrade": 88.75, "gradeMethod": "weighted", "gradedCount": 3, "gradedWeight": 40, "totalWeight": 100})
        plain = compute_grade([A(grade=72, maxGrade=None, weight=None), A(grade=45, maxGrade=50, weight=0)])
        self.assertEqual((plain["currentGrade"], plain["gradeMethod"]), (81, "average"))
        self.assertEqual(compute_grade([A(grade=None, maxGrade=None, weight=5)])["currentGrade"], None)
        self.assertEqual(compute_grade([A(grade=5, maxGrade=0, weight=5)])["gradedCount"], 0)  # max 0 is skipped

    def test_average_and_letters(self):
        from app.os.study import compute_average, percent_to_letter

        by_credits = compute_average([{"currentGrade": 90, "credits": 3}, {"currentGrade": 70, "credits": 1}, {"currentGrade": None, "credits": None}])
        self.assertEqual(by_credits, {"averageGrade": 85, "gpa": 3.2, "letter": "B", "creditWeighted": True, "gradedCourses": 2})
        plain = compute_average([{"currentGrade": 90, "credits": 3}, {"currentGrade": 70, "credits": None}])
        self.assertEqual((plain["averageGrade"], plain["creditWeighted"]), (80, False))
        self.assertEqual([percent_to_letter(p) for p in (93, 92.99, 60, 59.99)], ["A", "A-", "D-", "F"])


if __name__ == "__main__":
    unittest.main()
