import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents import API_KEY_ENV, ModelAgents, ScriptedAgents
from backends import DockerBackend
from cli import resolve_repository, resolve_script, write_results
from config import load_config
from repair import add_requirement, repair


ROOT = Path(__file__).resolve().parents[1]
BROKEN_REPO = (
    Path(__file__).resolve().parent
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
            self.assertEqual(
                (result_dir / "steps_to_run.txt").read_text(),
                "1. Create .venv\n2. Run the app\n",
            )
            saved_report = json.loads(
                (result_dir / "report.json").read_text())
            self.assertEqual(saved_report["fixed_files"], ["package/tool.py"])

    def test_timeout_is_reported_without_exposing_provider_message(self):
        config = load_config(ROOT / "config.json")
        with patch.dict(
                "os.environ", {API_KEY_ENV: "test-token"}), patch(
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

    def test_created_file_is_kept_on_success_and_removed_on_regression(self):
        script = "fn main() {}\n"
        create = {
            "action": {
                "action": "edit",
                "reason": "Add the build script Cargo.toml names",
                "files": {"build/main.rs": script},
            },
            "approved": True,
        }

        def repair_with(root, build_passes):
            backend = DockerBackend("rust:test")

            def run(command, cwd, timeout, network=False):
                # Tests always fail; only the build depends on the new file.
                if command == ["cargo", "test"]:
                    return command_result(1)
                if command != ["cargo", "build"]:
                    return command_result()
                return command_result(
                    0 if build_passes((Path(cwd) / "build/main.rs").is_file())
                    else 1)

            with patch.object(
                    backend, "availability_status", return_value=command_result()), patch.object(
                    backend, "image_status", return_value=command_result()), patch.object(
                    backend, "run", side_effect=run), patch(
                    "repair.make_backend", return_value=backend):
                return repair(
                    root, ScriptedAgents([create]), backend="docker", rounds=1)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "Cargo.toml").write_text("[package]\nbuild = \"build/main.rs\"\n")
            report = repair_with(root, build_passes=lambda created: created)
            self.assertFalse((root / "build").exists())
        self.assertEqual(report["status"], "execution_only")
        self.assertEqual(report["fixed_files"], {"build/main.rs": script})
        self.assertIn("--- /dev/null\n+++ b/build/main.rs", report["diff"])

        # A new file that breaks a passing build is deleted again.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "Cargo.toml").write_text("[package]\n")
            (root / "tests").mkdir()
            (root / "tests/cli.rs").write_text("#[test] fn fails() {}\n")
            report = repair_with(root, build_passes=lambda created: not created)
        self.assertTrue(report["history"][0]["rolled_back"])
        self.assertEqual(report["diff"], "")

if __name__ == "__main__":
    unittest.main()
