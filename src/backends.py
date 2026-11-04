"""Execution backends for trusted local runs and isolated Docker runs."""

from pathlib import Path

from execution import run_command


class LocalBackend:
    """Run a command directly on the host."""

    name = "local"

    def run(self, command, cwd, timeout, network=False):
        del network
        return run_command(command, cwd, timeout)


class DockerBackend:
    """Run commands in a resource-limited container without host credentials."""

    name = "docker"

    def __init__(self, image="python:3.12-slim"):
        self.image = image

    def image_status(self, cwd, timeout):
        """Check that the requested image already exists locally."""
        return run_command(
            ["docker", "image", "inspect", self.image], cwd, timeout)

    def availability_status(self, cwd, timeout):
        """Check that the Docker client can reach a running engine."""
        return run_command(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            cwd,
            timeout,
        )

    def pull(self, cwd, timeout):
        """Download the selected image after the caller has approved it."""
        return run_command(["docker", "pull", self.image], cwd, timeout)

    def command(self, command, cwd, network=False):
        """Build the auditable docker command used for one target command."""
        root = Path(cwd).resolve()
        network_mode = "bridge" if network else "none"
        return [
            "docker", "run", "--rm",
            "--network", network_mode,
            "--cpus", "2",
            "--memory", "2g",
            "--pids-limit", "256",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            # Docker Desktop maps some macOS temporary folders to container
            # root, so a host UID cannot create the disposable environment.
            # Root has no Linux capabilities and can write only the mounted
            # temporary work copy and the container's temporary filesystem.
            "--user", "0:0",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "PYTHONUNBUFFERED=1",
            "--env", "HOME=/tmp",
            "--env", "PIP_NO_CACHE_DIR=1",
            "--volume", str(root) + ":/workspace:rw",
            "--workdir", "/workspace",
            self.image,
            *[str(item) for item in command],
        ]

    def run(self, command, cwd, timeout, network=False):
        return run_command(
            self.command(command, cwd, network=network), cwd, timeout)


def make_backend(name, image="python:3.12-slim"):
    if name == "local":
        return LocalBackend()
    if name == "docker":
        return DockerBackend(image)
    raise ValueError("Unsupported execution backend: " + str(name))
