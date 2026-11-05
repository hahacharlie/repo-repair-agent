from pathlib import Path
import unittest
from unittest.mock import patch

from backends import DockerBackend, LocalBackend, make_backend
from repository import detect_repository, inspect_repository


class BackendTests(unittest.TestCase):
    def test_backend_selection(self):
        self.assertIsInstance(make_backend("local"), LocalBackend)
        self.assertIsInstance(make_backend("docker"), DockerBackend)
        with self.assertRaises(ValueError):
            make_backend("unknown")

    def test_docker_command_has_required_isolation(self):
        command = DockerBackend("python:3.12-slim").command(
            ["python", "main.py"], Path("/tmp/example"))
        joined = " ".join(command)
        for expected in (
                "--network none", "--read-only", "--cap-drop ALL",
                "--security-opt no-new-privileges", "--pids-limit 256",
                "--memory 2g", "--cpus 2"):
            self.assertIn(expected, joined)
        self.assertNotIn("docker.sock", joined)
        self.assertNotIn(str(Path.home()), joined)

    def test_network_is_opt_in_for_dependency_install(self):
        backend = DockerBackend()
        isolated = backend.command(["python", "main.py"], Path("/tmp/example"))
        installing = backend.command(
            ["python", "-m", "pip", "install", "x"],
            Path("/tmp/example"),
            network=True,
        )
        self.assertIn("none", isolated)
        self.assertIn("bridge", installing)

    def test_docker_status_and_pull_use_explicit_commands(self):
        calls = []

        def execute(command, cwd, timeout):
            calls.append(command)
            return {"exit_code": 0, "timeout": False}

        with patch("backends.run_command", side_effect=execute):
            backend = DockerBackend("python:test")
            backend.availability_status(Path("/tmp"), 10)
            backend.image_status(Path("/tmp"), 10)
            backend.pull(Path("/tmp"), 10)

        self.assertEqual(calls[0][:3], ["docker", "info", "--format"])
        self.assertEqual(calls[1], ["docker", "image", "inspect", "python:test"])
        self.assertEqual(calls[2], ["docker", "pull", "python:test"])

    def test_repository_markers_select_language_and_image(self):
        with self.subTest("go"):
            self.assertEqual(
                detect_repository(Path("tests/broken_repos/lazygit"))[:3],
                ("go", "go", "golang:bookworm"),
            )
            metadata = inspect_repository(
                Path("tests/broken_repos/lazygit"))
            self.assertEqual(metadata["language"], "go")
            self.assertEqual(metadata["entry"], "go.mod")
            self.assertEqual(metadata["build_command"], ["go", "build", "./..."])
        with self.subTest("python"):
            metadata = inspect_repository(
                Path("tests/broken_repos/undefined_greeting_variable"))
            self.assertEqual(metadata["language"], "python")
            self.assertEqual(metadata["docker_image"], "python:3.12-slim")


if __name__ == "__main__":
    unittest.main()
