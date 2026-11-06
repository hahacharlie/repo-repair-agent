import unittest

from metrics import evaluate_correct_repair, summarize_repair_rate


def command(exit_code=0):
    return {"exit_code": exit_code, "timeout": False}


class MetricsTests(unittest.TestCase):
    def test_clean_exit_without_tests_is_not_a_correct_repair(self):
        report = {
            "status": "execution_only",
            "initial": {"app": command(1), "tests": None},
            "final": {"app": command(), "tests": None},
        }

        outcome = evaluate_correct_repair(report)

        self.assertTrue(outcome["evaluated"])
        self.assertFalse(outcome["correct"])
        self.assertEqual(outcome["reason"], "No functional tests were found")
        self.assertEqual(summarize_repair_rate([report]), {
            "correct_repairs": 0,
            "evaluated_runs": 1,
            "percentage": 0.0,
            "note": "no_tests_found",
        })

    def test_environment_repair_counts_when_original_command_passes(self):
        report = {
            "status": "execution_only",
            "initial": {"app": command(1), "tests": None},
            "final": {"app": command(), "tests": None},
            "history": [{
                "action": {"action": "install", "package": "requests"},
                "judge": {"approved": True},
                "installation": command(),
                "observation": {"app": command(), "tests": None},
            }],
        }

        outcome = evaluate_correct_repair(report)

        self.assertTrue(outcome["correct"])
        self.assertEqual(summarize_repair_rate([report]), {
            "correct_repairs": 1,
            "evaluated_runs": 1,
            "percentage": 100.0,
        })

    def test_rate_combines_correct_failed_and_clean_runs(self):
        correct = {
            "status": "passed",
            "initial": {"app": command(1), "tests": command(1)},
            "final": {"app": command(), "tests": command()},
        }
        failed = {
            "status": "failed",
            "initial": {"app": command(1), "tests": command(1)},
            "final": {"app": command(), "tests": command(1)},
        }
        clean = {
            "status": "passed",
            "initial": {"app": command(), "tests": command()},
            "final": {"app": command(), "tests": command()},
        }

        rate = summarize_repair_rate([correct, failed, clean])

        self.assertEqual(rate, {
            "correct_repairs": 1,
            "evaluated_runs": 2,
            "percentage": 50.0,
        })


if __name__ == "__main__":
    unittest.main()
