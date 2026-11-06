from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from agents import ScriptedAgents
from execution import run_command
from repair import repair


BROKEN_REPO = (
    Path(__file__).resolve().parents[1]
    / "input"
    / "broken_repos"
    / "undefined_greeting_variable"
)
FIX = 'def greet(name):\n    return "Hello, " + name\n'


class ExecutionTests(unittest.TestCase):
    def test_command_reads_unicode_output(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_command(
                [
                    sys.executable,
                    "-c",
                    "import os, sys; "
                    "print('A → B'); "
                    "print(sys.executable); "
                    "print(os.getcwd())",
                ],
                directory,
                10,
            )

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["command"][0], "<python>")
        self.assertIn("A → B", result["stdout"])
        self.assertIn("<python>", result["stdout"])
        self.assertIn("<repo>", result["stdout"])
        self.assertNotIn(str(Path.home()), str(result))

    def test_uv_installs_into_target_environment(self):
        installations = []
        environments = []

        def execute(command, cwd, timeout):
            if command[:3] == ["uv", "--no-cache", "venv"]:
                environments.append((command, Path(cwd)))
            if command[:3] == ["uv", "pip", "install"]:
                installations.append(command)
                return {
                    "exit_code": 0,
                    "timeout": False,
                    "stdout": "",
                    "stderr": "",
                }
            return run_command(command, cwd, timeout)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            shutil.copytree(BROKEN_REPO, root)
            shutil.rmtree(root / ".venv", ignore_errors=True)
            (root / "requirements.txt").write_text("example-package\n")
            install = {
                "action": {
                    "action": "install",
                    "package": "another-package",
                    "reason": "Exercise target installation",
                },
                "approved": True,
            }
            edit = {
                "action": {
                    "action": "edit",
                    "reason": "Fix undefined variable",
                    "files": {"greeting.py": FIX},
                },
                "approved": True,
            }
            with patch(
                    "repair.run_command",
                    side_effect=execute):
                report = repair(
                    root,
                    ScriptedAgents([install, edit]),
                    allow_install=True,
                )
            self.assertEqual(report["status"], "passed")
            self.assertEqual(len(environments), 1)
            environment_command, environment_cwd = environments[0]
            self.assertIn("--clear", environment_command)
            self.assertEqual(environment_cwd, root)
            self.assertTrue((root / ".venv").exists())
            self.assertEqual(Path(environment_command[-1]), root / ".venv")
            self.assertEqual(len(installations), 2)
            self.assertEqual(installations[0][3], "--python")
            self.assertEqual(installations[0][4], installations[1][4])
            python_file = (
                "Scripts/python.exe"
                if sys.platform == "win32"
                else "bin/python"
            )
            self.assertEqual(
                Path(installations[0][4]),
                root / ".venv" / python_file,
            )
            self.assertEqual(
                installations[0][5:], ["-r", "requirements.txt"])
            self.assertEqual(installations[1][5:], ["another-package"])
            self.assertNotIn("judge", report["history"][0])
            self.assertFalse(
                report["history"][0]["approval_required"])
            self.assertTrue(
                report["history"][0]["requirements_updated"])
            self.assertEqual(
                report["fixed_files"]["requirements.txt"],
                "example-package\nanother-package\n",
            )
            self.assertIn("another-package", report["steps_to_run"])
            self.assertLess(
                report["steps_to_run"].find("fixed_files"),
                report["steps_to_run"].find("Install"),
            )

    def test_dependency_install_does_not_call_judge(self):
        installations = []

        def execute(command, cwd, timeout):
            if command[:3] == ["uv", "pip", "install"]:
                installations.append(command)
                return {
                    "exit_code": 0,
                    "timeout": False,
                    "stdout": "",
                    "stderr": "",
                }
            return run_command(command, cwd, timeout)

        action = {
            "action": {
                "action": "install",
                "package": "unneeded-package",
                "reason": "Try an unrelated dependency",
            },
            "approved": False,
        }
        with patch("repair.run_command", side_effect=execute):
            report = repair(
                BROKEN_REPO,
                ScriptedAgents([action]),
                rounds=1,
            )

        self.assertEqual(len(installations), 1)
        self.assertNotIn("judge", report["history"][0])
        self.assertFalse(report["history"][0]["approval_required"])


if __name__ == "__main__":
    unittest.main()
