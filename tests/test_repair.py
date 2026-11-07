import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents import ModelAgents, ScriptedAgents
from backends import DockerBackend, LocalBackend
from cli import resolve_repository, resolve_script, write_results
from config import load_config
import constants as ct
from repair import (
    add_requirement,
    has_mixed_cosmetic_changes,
    preserve_cosmetic_side_changes,
    repair,
    validate_repository_symlinks,
)


ROOT = Path(__file__).resolve().parents[1]
BROKEN_REPO = (
    ROOT
    / "input"
    / "broken_repos"
    / "undefined_greeting_variable"
)
FIX = 'def greet(name):\n    return "Hello, " + name\n'


def command_result(exit_code=0):
    return {
        "command": ["docker"],
        "exit_code": exit_code,
        "stdout": "",
        "stderr": "",
        "timeout": False,
    }


class FakeTokenCounter:
    def encoding_name(self):
        return "test_encoding"

    def count_prompt(self, model, messages):
        return 11

    def count_response(self, model, text):
        return 7


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
    def test_requirement_is_created_without_duplicate_names(self):
        self.assertEqual(add_requirement("", "requests"), "requests\n")
        self.assertEqual(
            add_requirement("PyYAML==6.0.3\n", "pyyaml"),
            "PyYAML==6.0.3\n",
        )

    def test_mixed_cosmetic_changes_are_detected(self):
        before = {"module.py": "value = broken\n\n"}
        self.assertTrue(has_mixed_cosmetic_changes(
            before, {"module.py": "value = fixed\n"}))
        self.assertFalse(has_mixed_cosmetic_changes(
            before, {"module.py": "value = fixed\n\n"}))
        self.assertFalse(has_mixed_cosmetic_changes(
            before, {"module.py": "value = broken\n"}))

    def test_cosmetic_side_changes_are_preserved(self):
        before = {"module.py": "value = broken\n\nnext_value = 1\n"}
        proposed = {"module.py": "value = fixed\nnext_value = 1\n"}
        self.assertEqual(
            preserve_cosmetic_side_changes(before, proposed),
            {"module.py": "value = fixed\n\nnext_value = 1\n"},
        )

    def test_pure_whitespace_fix_is_not_silently_removed(self):
        before = {"module.py": "if ready:\nprint('yes')\n"}
        proposed = {"module.py": "if ready:\n    print('yes')\n"}
        self.assertEqual(
            preserve_cosmetic_side_changes(before, proposed), proposed)

    def test_repository_symlinks_must_resolve_inside_the_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            root.mkdir()
            target = root / "target.txt"
            target.write_text("safe\n")
            link = root / "link.txt"
            link.symlink_to("target.txt")

            validate_repository_symlinks(root)

            link.unlink()
            outside = base / "outside.txt"
            outside.write_text("outside\n")
            ignored = root / ".git" / "config"
            ignored.parent.mkdir()
            ignored.write_text("ignored\n")
            cases = (
                (str(outside.resolve()), "absolute target"),
                ("missing.txt", "dangling target"),
                ("../outside.txt", "target escapes repository"),
                (".git/config", "target is ignored"),
            )
            for link_target, expected in cases:
                with self.subTest(link_target=link_target):
                    if link.exists() or link.is_symlink():
                        link.unlink()
                    link.symlink_to(link_target)
                    with self.assertRaisesRegex(ValueError, expected):
                        validate_repository_symlinks(root)

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
            "steps_to_run": "1. Create .venv\n2. Run the app",
            "agent_logs": [{
                "run": 1,
                "agent": "fixer",
                "prompt": {"system": "Fix it", "context": {}},
                "response": {"action": "give_up", "reason": "Done"},
            }],
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
            self.assertFalse((result_dir / "repair.patch").exists())
            self.assertEqual(
                (result_dir / "steps_to_run.txt").read_text(),
                "1. Create .venv\n2. Run the app\n",
            )
            saved_report = json.loads(
                (result_dir / "report.json").read_text())
            self.assertEqual(saved_report["fixed_files"], ["package/tool.py"])
            self.assertEqual(saved_report["agent_log_file"], "agent_logs.json")
            self.assertNotIn("agent_logs", saved_report)
            saved_logs = json.loads(
                (result_dir / "agent_logs.json").read_text())
            self.assertEqual(saved_logs, report["agent_logs"])

    def test_results_remove_a_legacy_patch(self):
        report = {
            "status": "failed",
            "reason": "Docker image is unavailable",
            "diff": "sample patch",
            "fixed_files": {},
            "steps_to_run": "Do not save failed-run instructions",
        }
        with tempfile.TemporaryDirectory() as directory:
            results = Path(directory)
            patch_path = results / "sample" / "repair.patch"
            patch_path.parent.mkdir(parents=True)
            patch_path.write_text("stale patch")
            steps_path = results / "sample" / "steps_to_run.txt"
            steps_path.write_text("stale instructions")

            result_dir = write_results(results, "sample", report)

            self.assertFalse((result_dir / "repair.patch").exists())
            self.assertFalse((result_dir / "steps_to_run.txt").exists())
            self.assertTrue((result_dir / "report.json").is_file())

    def test_timeout_is_reported_without_exposing_provider_message(self):
        config = load_config(ROOT / "config.json")
        with patch.dict(
                "os.environ", {ct.API_KEY_ENV: "test-token"}), patch(
                "agents.urllib.request.urlopen",
                side_effect=TimeoutError("private provider detail")):
            report = repair(
                BROKEN_REPO, ModelAgents(
                    config, capture_logs=True,
                    token_counter=FakeTokenCounter()))
        self.assertEqual(report["status"], "failed")
        self.assertIn("TimeoutError", report["reason"])
        self.assertEqual(
            report["model_requests"][0]["error_type"], "TimeoutError")
        self.assertEqual(
            report["agent_logs"][0]["error_type"], "TimeoutError")
        self.assertNotIn("private provider detail", str(report))
        self.assertEqual(report["diff"], "")

    def test_approved_repair_passes_and_original_is_unchanged(self):
        original = (BROKEN_REPO / "greeting.py").read_bytes()
        report = repair(BROKEN_REPO, ScriptedAgents([step()]))
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["metrics"]["correct_repair_rate"], {
            "correct_repairs": 1,
            "evaluated_runs": 1,
            "percentage": 100.0,
        })
        self.assertIn("+    return", report["diff"])
        self.assertEqual(report["fixed_files"], {"greeting.py": FIX})
        self.assertEqual(
            (BROKEN_REPO / "greeting.py").read_bytes(), original)

    def test_clean_repository_exits_without_agent_work(self):
        messages = []
        with patch(
                "repair.run_command", return_value=command_result()), patch(
                "repair.time.monotonic", side_effect=[100, 112.345]):
            report = repair(
                BROKEN_REPO, ScriptedAgents([]), progress=messages.append)

        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["no_repair_needed"])
        self.assertEqual(
            report["reason"], "No errors found; no repair was needed")
        self.assertEqual(report["history"], [])
        self.assertEqual(report["diff"], "")
        self.assertEqual(report["fixed_files"], {})
        self.assertNotIn("steps_to_run", report)
        self.assertEqual(report["metrics"], {
            "wall_clock_seconds": 12.34,
            "model_api_seconds": 0.0,
            "model_request_count": 0,
            "token_usage": {
                "source": "no_model_requests",
                "available": True,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
            "token_usage_by_agent": {
                "fixer": {
                    "source": "no_model_requests",
                    "available": True,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
                "judge": {
                    "source": "no_model_requests",
                    "available": True,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            },
            "correct_repair_rate": {
                "correct_repairs": 0,
                "evaluated_runs": 0,
                "percentage": None,
            },
        })
        self.assertEqual([message.strip() for message in messages], [
            "Preparing temporary work copy...",
            "Preparing Python environment...",
            "Running application...",
            "Running tests...",
        ])

    def test_rejected_patch_never_applies_and_limit_stops(self):
        report = repair(
            BROKEN_REPO, ScriptedAgents([step(False)]), rounds=1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["metrics"]["correct_repair_rate"], {
            "correct_repairs": 0,
            "evaluated_runs": 1,
            "percentage": 0.0,
        })
        self.assertEqual(report["diff"], "")
        self.assertEqual(report["fixed_files"], {})
        self.assertEqual(len(report["history"]), 1)

    def test_give_up_does_not_call_judge(self):
        action = {
            "action": {
                "action": "give_up",
                "reason": "A human must update the environment",
            },
            "approved": False,
        }

        report = repair(BROKEN_REPO, ScriptedAgents([action]), rounds=1)

        self.assertEqual(
            report["reason"], "A human must update the environment")
        self.assertNotIn("judge", report["history"][0])

    def test_unsupported_install_stops_without_judge(self):
        backend = DockerBackend("meson:test")

        def run_in_container(command, cwd, timeout, network=False):
            del cwd, timeout, network
            if command == ["test", "-f", "meson.build"]:
                return command_result()
            if command == ["mkdir", "-p", ".home/tmp"]:
                return command_result()
            return {
                **command_result(127),
                "command": command,
                "stderr": "sh: 1: meson: not found\n",
            }

        install = {
            "action": {
                "action": "install",
                "package": "meson",
                "reason": "Missing build dependency meson",
            },
            "approved": True,
        }
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "meson.build").write_text("project('sample', 'c')\n")
            with patch.object(
                    backend, "availability_status",
                    return_value=command_result()), patch.object(
                    backend, "image_status",
                    return_value=command_result()), patch.object(
                    backend, "run", side_effect=run_in_container), patch(
                    "repair.make_backend", return_value=backend):
                report = repair(
                    root, ScriptedAgents([install]), rounds=30, backend="auto")

        self.assertEqual(len(report["history"]), 1)
        self.assertNotIn("judge", report["history"][0])
        self.assertFalse(report["history"][0]["approval_required"])
        self.assertEqual(
            report["history"][0]["error"],
            "Dependency installation unsupported for meson",
        )
        self.assertEqual(
            report["reason"],
            "Dependency installation unsupported for meson",
        )

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

    def test_docker_unavailable_stops_before_repair_loop(self):
        backend = DockerBackend("python:test")
        with patch.object(
                backend, "availability_status", return_value=command_result(1)), patch(
                "repair.make_backend", return_value=backend):
            report = repair(
                BROKEN_REPO, ScriptedAgents([]), backend="docker")

        self.assertEqual(report["status"], "failed")
        self.assertIn("Docker is unavailable", report["reason"])
        self.assertEqual(report["execution_backend"], "docker")

    def test_missing_docker_image_requires_explicit_approval(self):
        backend = DockerBackend("python:test")
        with patch.object(
                backend, "availability_status", return_value=command_result()), patch.object(
                backend, "image_status", return_value=command_result(1)), patch(
                "repair.make_backend", return_value=backend):
            report = repair(
                BROKEN_REPO, ScriptedAgents([]), backend="docker")

        self.assertTrue(report["docker"]["pull_required"])
        self.assertIn("--allow-docker-pull", report["reason"])

    def test_docker_runs_only_the_temporary_copy(self):
        backend = DockerBackend("python:test")
        calls = []

        def run_in_container(command, cwd, timeout, network=False):
            calls.append((command, Path(cwd), network))
            return command_result()

        with patch.object(
                backend, "availability_status", return_value=command_result()), patch.object(
                backend, "image_status", return_value=command_result()), patch.object(
                backend, "run", side_effect=run_in_container), patch(
                "repair.make_backend", return_value=backend):
            report = repair(
                BROKEN_REPO, ScriptedAgents([]), backend="docker")

        self.assertEqual(report["status"], "passed")
        self.assertTrue(calls)
        self.assertTrue(all(cwd != BROKEN_REPO for _, cwd, _ in calls))
        self.assertEqual(calls[0][0], ["test", "-f", "main.py"])
        self.assertEqual(calls[1][0], ["python", "-m", "venv", ".venv"])
        self.assertTrue(all(not network for _, _, network in calls))

    def test_missing_required_python_falls_back_to_docker(self):
        docker = DockerBackend("python:3.11-slim")
        local = LocalBackend()
        docker_calls = []

        def choose_backend(name, image=None):
            if name == "local":
                return local
            self.assertEqual(image, "python:3.11-slim")
            return docker

        def local_command(command, cwd, timeout):
            self.assertEqual(command[:3], ["uv", "python", "find"])
            self.assertIn("3.11", command)
            return command_result(1)

        def docker_command(command, cwd, timeout, network=False):
            docker_calls.append(command)
            return command_result()

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "main.py").write_text("print('ok')\n")
            (root / ".python-version").write_text("3.11\n")
            with patch(
                    "repair.run_command", side_effect=local_command), patch(
                    "repair.make_backend", side_effect=choose_backend), patch.object(
                    docker, "availability_status",
                    return_value=command_result()), patch.object(
                    docker, "image_status",
                    return_value=command_result()), patch.object(
                    docker, "run", side_effect=docker_command):
                report = repair(root, ScriptedAgents([]), backend="auto")

        self.assertEqual(report["status"], "execution_only")
        self.assertEqual(report["execution_backend"], "docker")
        self.assertIn("3.11", report["docker_fallback"])
        self.assertEqual(report["docker"]["image"], "python:3.11-slim")
        self.assertEqual(docker_calls[:3], [
            ["test", "-f", "main.py"],
            ["python", "-m", "venv", ".venv"],
            ["/workspace/.venv/bin/python", "main.py"],
        ])

    def test_cargo_repository_builds_and_tests_in_docker(self):
        backend = DockerBackend("rust:test")
        calls = []

        def run_in_container(command, cwd, timeout, network=False):
            calls.append((command, network))
            # The first build fails until the Fixer's edit is applied.
            failing = command == ["cargo", "build"] and not (
                Path(cwd) / "src/main.rs").read_text().startswith("fn main")
            return command_result(1 if failing else 0)

        fix = {
            "action": {
                "action": "edit",
                "reason": "Add missing main",
                "files": {"src/main.rs": "fn main() {}\n"},
            },
            "approved": True,
        }
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "Cargo.toml").write_text("[package]\nname = \"demo\"\n")
            (root / "src/main.rs").write_text("broken\n")
            (root / "tests/cli.rs").write_text("#[test] fn ok() {}\n")
            with patch.object(
                    backend, "availability_status", return_value=command_result()), patch.object(
                    backend, "image_status", return_value=command_result()), patch.object(
                    backend, "run", side_effect=run_in_container), patch(
                    "repair.make_backend", return_value=backend):
                report = repair(root, ScriptedAgents([fix]), backend="auto")
            self.assertEqual((root / "src/main.rs").read_text(), "broken\n")

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["execution_backend"], "docker")
        self.assertEqual(sorted(report["fixed_files"]), ["src/main.rs"])
        self.assertEqual(calls[:5], [
            (["test", "-f", "Cargo.toml"], False),
            (["mkdir", "-p", ".home/tmp"], False),
            (["cargo", "fetch"], True),
            (["cargo", "build"], False),
            (["cargo", "test"], False),
        ])
        self.assertIn("cargo build", report["steps_to_run"])

    def test_readme_command_failure_reaches_fixer(self):
        backend = DockerBackend("rust:test")
        plan = {
            "dependency_command": ["project-tool", "prepare"],
            "build_command": ["project-tool", "build"],
            "test_command": ["project-tool", "test"],
        }
        calls = []

        def run_in_container(command, cwd, timeout, network=False):
            del cwd, timeout
            calls.append((command, network))
            result = command_result(
                1 if command == plan["dependency_command"] else 0)
            result["command"] = command
            if result["exit_code"]:
                result["stderr"] = "dependency preparation failed"
            return result

        readme = (
            "Use `project-tool prepare`, then `project-tool build`, and "
            "`project-tool test`.\n")
        give_up = {
            "action": "give_up",
            "reason": "The documented dependency command failed",
        }
        with patch.dict(
                os.environ, {ct.API_KEY_ENV: "test-token"}):
            agents = ModelAgents(load_config(ROOT / "config.json"))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "src").mkdir()
            (root / "Cargo.toml").write_text("[package]\nname = \"demo\"\n")
            (root / "README.md").write_text(readme)
            (root / "src/main.rs").write_text("fn main() {}\n")
            with patch.object(
                    agents, "plan_commands", return_value=plan) as planner, patch.object(
                    agents, "propose", return_value=give_up) as propose, patch.object(
                    agents, "generate_run_steps", return_value="Run documented commands"), patch.object(
                    backend, "availability_status",
                    return_value=command_result()), patch.object(
                    backend, "image_status",
                    return_value=command_result()), patch.object(
                    backend, "run", side_effect=run_in_container), patch(
                    "repair.make_backend", return_value=backend):
                report = repair(root, agents, rounds=1, backend="auto")

        planning_context = planner.call_args.args[0]
        self.assertEqual(planning_context["readme"], readme)
        self.assertEqual(
            planning_context["root_files"]["Cargo.toml"],
            "[package]\nname = \"demo\"\n",
        )
        context = propose.call_args.args[0]
        self.assertEqual(context["repository"]["build_command"], plan["build_command"])
        self.assertEqual(context["repository"]["test_command"], plan["test_command"])
        self.assertEqual(
            context["observation"]["app"]["command"],
            plan["dependency_command"],
        )
        self.assertIn((plan["dependency_command"], True), calls)
        self.assertNotIn((plan["build_command"], False), calls)
        self.assertEqual(report["reason"], give_up["reason"])


if __name__ == "__main__":
    unittest.main()
