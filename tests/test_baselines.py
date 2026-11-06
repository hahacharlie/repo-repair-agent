import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents import ScriptedAgents
from baselines import (
    FixerOnlyAgents,
    STAKEHOLDER_FIXER_MODEL,
    STAKEHOLDER_JUDGE_MODEL,
    StakeholderAgents,
    parse_stakeholder_fixer,
    parse_stakeholder_judge,
)
from config import load_config
import constants as ct
from repair import repair


ROOT = Path(__file__).resolve().parents[1]
BROKEN_REPO = ROOT / "input" / "broken_repos" / "undefined_greeting_variable"
FIX = 'def greet(name):\n    return "Hello, " + name\n'


def command_result(exit_code=0):
    return {
        "command": ["test"],
        "exit_code": exit_code,
        "stdout": "",
        "stderr": "missing package" if exit_code else "",
        "timeout": False,
    }


class StakeholderBaselineTests(unittest.TestCase):
    def test_fixer_only_uses_proposed_fixer_without_judge(self):
        config = load_config(ROOT / "config.json")
        with patch.dict(os.environ, {ct.API_KEY_ENV: "test-token"}):
            live_agents = FixerOnlyAgents(config)
        self.assertEqual(live_agents.baseline_name, "fixer-only")

        steps = [{
            "action": {
                "action": "edit",
                "reason": "Fix the undefined variable",
                "files": {"greeting.py": FIX},
            },
            "approved": False,
        }]
        agents = ScriptedAgents(steps)
        agents.baseline_name = "fixer-only"

        report = repair(BROKEN_REPO, agents, rounds=1)

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["pipeline"], "fixer-only")
        self.assertNotIn("judge", report["history"][0])
        self.assertFalse(report["history"][0]["approval_required"])

    def test_stakeholder_response_formats_are_converted(self):
        self.assertEqual(
            parse_stakeholder_fixer(
                "ACTION: COMMAND\nDETAIL: pip install requests"),
            {
                "action": "install",
                "package": "requests",
                "reason": "Stakeholder Fixer command: pip install requests",
            },
        )
        edit = parse_stakeholder_fixer(
            "ACTION: EDIT\nFILE: main.py\n"
            "JUSTIFICATION: Fix the name\nCONTENT:\nprint('fixed')\n"
        )
        self.assertEqual(edit["files"], {"main.py": "print('fixed')\n"})
        self.assertEqual(
            parse_stakeholder_fixer(
                "ACTION: REPORT\nDETAIL: Needs a human")["action"],
            "give_up",
        )
        self.assertTrue(parse_stakeholder_judge(
            "VERDICT: APPROVE\nREASON: Behavior stays the same")["approved"])

    def test_stakeholder_uses_original_models_and_text_prompts(self):
        config = load_config(ROOT / "config.json")
        context = {
            "repository": {"entry": "main.py"},
            "files": {
                "README.md": "Small greeting app",
                "main.py": "print(name)\n",
            },
            "observation": {"app": command_result(1), "tests": None},
            "history": [],
        }
        with patch.dict(
                os.environ, {ct.API_KEY_ENV: "test-token"}), patch.object(
                StakeholderAgents,
                "request_text",
                side_effect=[
                    "ACTION: EDIT\nFILE: main.py\n"
                    "JUSTIFICATION: Fix the name\nCONTENT:\n"
                    "print('fixed')\n",
                    "VERDICT: APPROVE\nREASON: Output is preserved",
                ],
                ) as request:
            agents = StakeholderAgents(config)
            action = agents.propose(context)
            review = agents.review({
                "before": {"main.py": "print(name)\n"},
                "after": action["files"],
                "reason": action["reason"],
            })

        self.assertTrue(review["approved"])
        self.assertEqual(request.call_args_list[0].args[0], STAKEHOLDER_FIXER_MODEL)
        self.assertEqual(request.call_args_list[1].args[0], STAKEHOLDER_JUDGE_MODEL)
        self.assertIn("Small greeting app", request.call_args_list[0].args[2])

    def test_stakeholder_install_skips_judge_and_counts_as_repair(self):
        steps = [{
            "action": {
                "action": "install",
                "package": "requests",
                "reason": "Stakeholder Fixer command: pip install requests",
            },
            "approved": False,
        }]
        agents = ScriptedAgents(steps)
        agents.baseline_name = "stakeholder"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text("print('ready')\n")
            with patch(
                    "repair.run_command",
                    side_effect=[
                        command_result(),
                        command_result(1),
                        command_result(),
                        command_result(),
                    ]):
                report = repair(root, agents, rounds=1)

        record = report["history"][0]
        self.assertNotIn("judge", record)
        self.assertFalse(record["approval_required"])
        self.assertEqual(report["pipeline"], "stakeholder")
        self.assertEqual(report["metrics"]["correct_repair_rate"], {
            "correct_repairs": 1,
            "evaluated_runs": 1,
            "percentage": 100.0,
        })
        self.assertEqual(report["fixed_files"], {})


if __name__ == "__main__":
    unittest.main()
