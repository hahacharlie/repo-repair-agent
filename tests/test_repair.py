import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents import ModelAgents, ScriptedAgents
from cli import resolve_repository, resolve_script, write_results
from config import load_config
from repair import repair


ROOT = Path(__file__).resolve().parents[1]
BROKEN_REPO = (
    Path(__file__).resolve().parent
    / "broken_repos"
    / "undefined_greeting_variable"
)
FIX = 'def greet(name):\n    return "Hello, " + name\n'


def step(approved=True):
    return {
        "action": {
            "action": "edit",
            "reason": "Fix undefined variable",
            "files": {"greeting.py": FIX},
        },
        "approved": approved,
    }


class RepairTests(unittest.TestCase):
    def test_saved_repo_can_be_selected_by_folder_name(self):
        selected = resolve_repository(Path("undefined_greeting_variable"))
        self.assertEqual(selected, BROKEN_REPO.resolve())
        self.assertEqual(
            resolve_script(selected, None), selected / "repair.json")

    def test_results_save_only_fixed_files(self):
        report = {
            "status": "passed",
            "reason": "Done",
            "diff": "sample patch",
            "fixed_files": {"package/tool.py": "print('fixed')\n"},
        }
        with tempfile.TemporaryDirectory() as directory:
            results = Path(directory)
            old_file = results / "sample" / "fixed_files" / "old.py"
            old_file.parent.mkdir(parents=True)
            old_file.write_text("old\n")

            result_dir = write_results(results, "sample", report)

            self.assertFalse(old_file.exists())
            self.assertEqual(
                (result_dir / "fixed_files/package/tool.py").read_text(),
                "print('fixed')\n",
            )
            self.assertEqual(
                (result_dir / "repair.patch").read_text(), "sample patch")
            saved_report = json.loads(
                (result_dir / "report.json").read_text())
            self.assertEqual(saved_report["fixed_files"], ["package/tool.py"])

    def test_timeout_is_reported_without_exposing_provider_message(self):
        config = load_config(ROOT / "config.json")
        with patch.dict(
                "os.environ", {config["api_key_env"]: "test-token"}), patch(
                "agents.urllib.request.urlopen",
                side_effect=TimeoutError("private provider detail")):
            report = repair(BROKEN_REPO, ModelAgents(config))
        self.assertEqual(report["status"], "failed")
        self.assertIn("TimeoutError", report["reason"])
        self.assertEqual(
            report["model_requests"][0]["error_type"], "TimeoutError")
        self.assertNotIn("private provider detail", str(report))
        self.assertEqual(report["diff"], "")

    def test_approved_repair_passes_and_original_is_unchanged(self):
        original = (BROKEN_REPO / "greeting.py").read_bytes()
        report = repair(BROKEN_REPO, ScriptedAgents([step()]))
        self.assertEqual(report["status"], "passed")
        self.assertIn("+    return", report["diff"])
        self.assertEqual(report["fixed_files"], {"greeting.py": FIX})
        self.assertEqual(
            (BROKEN_REPO / "greeting.py").read_bytes(), original)

    def test_rejected_patch_never_applies_and_limit_stops(self):
        report = repair(
            BROKEN_REPO, ScriptedAgents([step(False)]), rounds=1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["diff"], "")
        self.assertEqual(report["fixed_files"], {})
        self.assertEqual(len(report["history"]), 1)

    def test_regression_is_rolled_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text("print('ok')\n")
            (root / "test_main.py").write_text(
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_failure(self): self.fail('preexisting')\n"
            )
            action = {
                "action": {
                    "action": "edit",
                    "reason": "harmful",
                    "files": {
                        "main.py": "raise RuntimeError('regression')\n",
                    },
                },
                "approved": True,
            }
            report = repair(
                root, ScriptedAgents([action]), rounds=1)
            self.assertTrue(report["history"][0]["rolled_back"])
            self.assertEqual(report["diff"], "")


if __name__ == "__main__":
    unittest.main()
