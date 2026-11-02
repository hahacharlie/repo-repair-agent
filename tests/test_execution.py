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
    Path(__file__).resolve().parent
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

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            shutil.copytree(BROKEN_REPO, root)
            (root / "requirements.txt").write_text("example-package\n")
            install = {
                "action": {
                    "action": "install",
                    "package": "another-package",
                    "reason": "Exercise target installation",
                },
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
            self.assertEqual(len(installations), 2)
            self.assertEqual(installations[0][3], "--python")
            self.assertEqual(installations[0][4], installations[1][4])
            self.assertIn("env", Path(installations[0][4]).parts)
            self.assertEqual(
                installations[0][5:], ["-r", "requirements.txt"])
            self.assertEqual(installations[1][5:], ["another-package"])


if __name__ == "__main__":
    unittest.main()
