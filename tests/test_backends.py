from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backends import DockerBackend, LocalBackend, make_backend
import constants as ct
from repository import detect_repository, inspect_repository


class BackendTests(unittest.TestCase):
    def test_backend_selection(self):
        self.assertIsInstance(make_backend("local"), LocalBackend)
        self.assertIsInstance(make_backend("docker"), DockerBackend)
        with self.assertRaises(ValueError):
            make_backend("unknown")

    def test_container_keeps_host_credentials_out(self):
        root = Path("/tmp/example").resolve()
        command = DockerBackend("python:3.12-slim").start_command(
            root, "repo-repair-test")
        joined = " ".join(command)
        for expected in (
                "--network bridge", "--security-opt no-new-privileges",
                "--pids-limit", "--memory", "--cpus",
                str(root) + ":/workspace/example:rw",
                str(root.parent / ".sandbox-home") + ":/home/repair:rw",
                "HOME=/home/repair",
                "sleep infinity"):
            self.assertIn(expected, joined)
        self.assertNotIn("docker.sock", joined)
        self.assertNotIn("--privileged", joined)
        self.assertNotIn(str(Path.home()), joined)

    def test_commands_use_the_build_user_unless_another_is_requested(self):
        backend = DockerBackend()
        backend.container = "repo-repair-test"
        cwd = Path("/tmp/example")
        build = backend.exec_command(["make"], cwd, 30)
        root = backend.exec_command(["apt-get", "update"], cwd, 30, "root")
        reader = backend.exec_command(["ls"], cwd, 30, "reader")
        self.assertEqual(
            build[build.index("--user") + 1], backend.build_user())
        self.assertEqual(root[root.index("--user") + 1], "0:0")
        self.assertEqual(reader[reader.index("--user") + 1], "65534:65534")
        # The time limit must stop the processes inside the container.
        limit = build.index("timeout")
        self.assertEqual(build[limit:limit + 4], ["timeout", "-k", "10", "30"])
        self.assertEqual(build[-1], "make")
        self.assertIn("--workdir=/workspace/example", build)

    def test_one_container_serves_every_command_and_is_removed(self):
        calls = []

        def execute(command, cwd, timeout, on_timeout=None):
            calls.append(command)
            return {
                "exit_code": 0, "timeout": False, "stdout": "", "stderr": ""}

        with tempfile.TemporaryDirectory() as folder, patch(
                "backends.uuid.uuid4") as uuid4, patch(
                "backends.run_command", side_effect=execute):
            uuid4.return_value.hex = "abc123"
            backend = DockerBackend()
            cwd = Path(folder) / "example"
            first = backend.run(["go", "build"], cwd, 30)
            backend.run(["go", "test", "./..."], cwd, 30)
            backend.close()
            # The home folder of the build user is beside the work copy.
            self.assertTrue((Path(folder) / ".sandbox-home").is_dir())

        starts = [call for call in calls if call[:2] == ["docker", "run"]]
        self.assertEqual(len(starts), 1)
        self.assertIn("repo-repair-abc123", starts[0])
        self.assertEqual(first["command"], ["go", "build"])
        self.assertEqual(
            calls[-1], ["docker", "rm", "-f", "repo-repair-abc123"])
        self.assertIsNone(backend.container)

    def test_time_limit_inside_the_container_is_reported(self):
        backend = DockerBackend()
        backend.container = "repo-repair-test"
        backend.cwd = Path("/tmp/example")
        stopped = {
            "exit_code": 124, "timeout": False, "stdout": "", "stderr": ""}
        with patch("backends.run_command", return_value=stopped), patch(
                "backends.time.monotonic", side_effect=[0.0, 31.0]):
            result = backend.run(
                ["go", "test", "./..."], Path("/tmp/example"), 30)
        self.assertTrue(result["timeout"])

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
                ("go", "go", ct.SANDBOX_IMAGE),
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
