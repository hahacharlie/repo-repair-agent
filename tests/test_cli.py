import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cli import format_metrics_table, main, python_version_error


class CliTests(unittest.TestCase):
    def test_old_python_stops_before_cli_setup(self):
        message = python_version_error((3, 9, 18))
        self.assertIn("requires Python 3.10 or newer", message)
        self.assertIsNone(python_version_error((3, 10, 0)))

        output = io.StringIO()
        with redirect_stderr(output):
            result = main((3, 9, 18))
        self.assertEqual(result, 2)
        self.assertEqual(output.getvalue(), message)

    def test_agent_timeout_must_be_within_cli_range(self):
        output = io.StringIO()
        with patch(
                "sys.argv", [
                    "repo-repair-agent", "unused",
                    "--agent-timeout", "3601",
                ]), redirect_stderr(output), self.assertRaises(SystemExit) as error:
            main((3, 10, 0))
        self.assertEqual(error.exception.code, 2)
        self.assertIn("--agent-timeout must be from 1 to 3600", output.getvalue())

    def test_clean_repository_reports_no_error_and_returns_zero(self):
        report = {
            "status": "passed",
            "reason": "No errors found; no repair was needed",
            "no_repair_needed": True,
            "repository": {"language": "python"},
            "final": {
                "app": {"exit_code": 0, "timeout": False},
                "tests": {"exit_code": 0, "timeout": False},
            },
            "fixed_files": {},
            "metrics": {
                "wall_clock_seconds": 12.34,
                "model_api_seconds": 4.5,
                "token_usage": {"total_tokens": 26},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "clean_repo"
            repository.mkdir()
            (repository / "repair.json").write_text("[]\n")
            result_dir = Path(directory) / "results" / "clean_repo"
            output = io.StringIO()
            with patch(
                    "sys.argv", ["repo-repair-agent", str(repository)]), patch(
                    "cli.repair", return_value=report), patch(
                    "cli.write_results", return_value=result_dir), redirect_stdout(
                    output):
                result = main((3, 10, 0))

        self.assertEqual(result, 0)
        self.assertIn(
            "No errors found. No repair was needed.", output.getvalue())
        self.assertIn("Fixed files: none", output.getvalue())
        self.assertNotIn("Metrics Details", output.getvalue())


    def test_metrics_table_needs_success_and_valid_agent_values(self):
        report = {
            "status": "passed",
            "metrics": {
                "wall_clock_seconds": 12.34,
                "token_usage": {"total_tokens": 1801},
                "token_usage_by_agent": {
                    "fixer": {"total_tokens": 1234},
                    "judge": {"total_tokens": 567},
                },
            },
        }

        table = format_metrics_table(report)

        self.assertEqual(table, "\n".join([
            "+-----------------+------------------+",
            "| Metrics Details | Value            |",
            "+-----------------+------------------+",
            "| Fixer tokens    | 1,234            |",
            "+-----------------+------------------+",
            "| Judge tokens    | 567              |",
            "+-----------------+------------------+",
            "| Total tokens    | 1,801            |",
            "+-----------------+------------------+",
            "| Total run time  | 12.34s           |",
            "+-----------------+------------------+",
        ]))
        report["status"] = "failed"
        self.assertIsNone(format_metrics_table(report))
        report["status"] = "passed"
        report["metrics"]["token_usage_by_agent"]["judge"][
            "total_tokens"] = None
        self.assertIsNone(format_metrics_table(report))


if __name__ == "__main__":
    unittest.main()
