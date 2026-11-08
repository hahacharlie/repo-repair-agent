import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from backends import (
    DockerBackend, LocalBackend, host_cpus, host_memory_limit, make_backend,
)
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
                "--network bridge", "--read-only", "--cap-drop ALL",
                "--security-opt no-new-privileges", "--pids-limit 4096",
                "--memory " + host_memory_limit(),
                "--cpus " + str(host_cpus()),
                "/tmp/.sandbox-tmp:/tmp:rw",
                "/tmp/example:/workspace/example:rw",
                "--workdir /workspace/example",
                "HOME=/workspace/example/.home"):
            self.assertIn(expected, joined)
        self.assertRegex(host_memory_limit(), r"^\d+g$")
        self.assertNotIn("docker.sock", joined)
        self.assertNotIn(str(Path.home()), joined)
        if sys.platform == "linux":
            self.assertIn("/tmp/example/.home/passwd:/etc/passwd:ro", joined)

    def test_run_writes_a_passwd_entry_for_the_sandbox_user(self):
        if sys.platform != "linux":
            self.skipTest("host UID mapping is Linux only")
        with tempfile.TemporaryDirectory() as folder, patch(
                "backends.run_command",
                return_value={"exit_code": 0, "timeout": False}):
            # The work copy sits inside its own temporary folder, as in repair.
            work = Path(folder) / "repo"
            work.mkdir()
            DockerBackend().run(["true"], work, 10)
            text = (work / ".home" / "passwd").read_text()
            self.assertTrue((Path(folder) / ".sandbox-tmp").is_dir())
        self.assertIn(f"repair:x:{os.getuid()}:{os.getgid()}:", text)
        self.assertIn("/workspace/repo/.home", text)

    def test_every_command_gets_network_access(self):
        backend = DockerBackend()
        for network in (False, True):
            command = backend.command(
                ["python", "main.py"], Path("/tmp/example"), network=network)
            self.assertNotIn("none", command)
            self.assertIn("bridge", command)

    def test_timeout_force_removes_the_named_container(self):
        calls = []

        def execute(command, cwd, timeout, on_timeout=None):
            calls.append((command, cwd, timeout))
            if on_timeout is not None:
                on_timeout()
            return {"exit_code": -9, "timeout": True}

        with patch("backends.uuid.uuid4") as uuid4, patch(
                "backends.run_command", side_effect=execute):
            uuid4.return_value.hex = "abc123"
            DockerBackend().run(
                ["go", "test", "./..."], Path("/tmp/example"), 30)

        first_command = calls[0][0]
        name_index = first_command.index("--name")
        self.assertEqual(
            first_command[name_index + 1], "repo-repair-abc123")
        self.assertEqual(
            calls[1][0],
            ["docker", "rm", "-f", "repo-repair-abc123"],
        )
        self.assertEqual(calls[1][2], 10)

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
                detect_repository(Path("input/broken_repos/lazygit"))[:3],
                ("go", "go", "golang:bookworm"),
            )
            metadata = inspect_repository(
                Path("input/broken_repos/lazygit"))
            self.assertEqual(metadata["language"], "go")
            self.assertEqual(metadata["entry"], "go.mod")
            self.assertEqual(metadata["build_command"], ["go", "build", "./..."])
        with self.subTest("python"):
            metadata = inspect_repository(
                Path("input/broken_repos/undefined_greeting_variable"))
            self.assertEqual(metadata["language"], "python")
            self.assertEqual(metadata["docker_image"], "python:3.12-slim")


if __name__ == "__main__":
    unittest.main()
