"""Execution backends for trusted local runs and isolated Docker runs."""

import os
from pathlib import Path
import sys
import uuid

import constants as ct
from execution import run_command


class LocalBackend:
    """Run a command directly on the host."""

    name = "local"

    def run(self, command, cwd, timeout, network=False):
        del network
        return run_command(command, cwd, timeout)


def host_cpus():
    """Give builds every host CPU; large repositories need them."""
    return os.cpu_count() or 2


def host_memory_limit():
    """Allow three quarters of host memory, with a 2g floor."""
    try:
        total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return "2g"
    return str(max(2, total * 3 // 4 // 2**30)) + "g"


class DockerBackend:
    """Run commands in a resource-limited container without host credentials."""

    name = "docker"

    def __init__(self, image=ct.DEFAULT_PYTHON_IMAGE):
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

    def dockerfile(self):
        """Return the project Dockerfile that defines this image, if any."""
        if not self.image.startswith(ct.LOCAL_IMAGE_PREFIX):
            return None
        name = self.image[len(ct.LOCAL_IMAGE_PREFIX):].split(":")[0]
        path = ct.DOCKERFILES / (name + ".Dockerfile")
        return path if path.is_file() else None

    def build_image(self, cwd, timeout):
        """Build the image from the project's own Dockerfile."""
        return run_command(
            ["docker", "build", "--tag", self.image,
             "--file", str(self.dockerfile()), str(ct.DOCKERFILES)],
            cwd,
            timeout,
        )

    def passwd_file(self, cwd):
        """Return the passwd file that names the sandbox user (Linux only)."""
        return Path(cwd).resolve() / ".home" / "passwd"

    def container_root(self, cwd):
        # Keep the repository's own folder name: some projects check it.
        return "/workspace/" + Path(cwd).resolve().name

    def write_passwd_file(self, cwd):
        # Programs and tests call getpwuid for the current user, so the host
        # UID used inside the container needs a passwd entry.
        path = self.passwd_file(cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        home = self.container_root(cwd) + "/.home"
        path.write_text(
            "root:x:0:0:root:/root:/bin/sh\n"
            f"repair:x:{os.getuid()}:{os.getgid()}:repair:{home}:/bin/sh\n")

    def scratch_dir(self, cwd):
        """Return the disk folder mounted as /tmp, beside the work copy."""
        return Path(cwd).resolve().parent / ".sandbox-tmp"

    def command(self, command, cwd, network=False, container_name=None):
        """Build the auditable docker command used for one target command."""
        root = Path(cwd).resolve()
        workdir = self.container_root(cwd)
        # Builds and tests may download modules, fixtures, and interpreters,
        # so every command gets network access.
        del network
        docker_command = ["docker", "run", "--rm"]
        if container_name:
            docker_command.extend(["--name", container_name])
        if sys.platform == "linux":
            docker_command.extend([
                "--volume", str(self.passwd_file(cwd)) + ":/etc/passwd:ro"])
        return docker_command + [
            "--network", "bridge",
            "--cpus", str(host_cpus()),
            "--memory", host_memory_limit(),
            "--pids-limit", "4096",
            "--read-only",
            # Test suites and configure scripts run binaries from /tmp, and
            # go test writes gigabytes of build output there, so /tmp is a
            # disk folder beside the work copy, not memory.
            "--volume", str(self.scratch_dir(cwd)) + ":/tmp:rw",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            # Docker Desktop maps some macOS temporary folders to container
            # root, so a host UID cannot create the disposable environment.
            # Root has no Linux capabilities and can write only the mounted
            # temporary work copy and the container's temporary filesystem.
            # On a Linux engine that same lack of capabilities stops root from
            # writing the host user's folder, so use the host UID there.
            "--user", (
                f"{os.getuid()}:{os.getgid()}" if sys.platform == "linux"
                else "0:0"),
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "PYTHONUNBUFFERED=1",
            # The images point CARGO_HOME/GOPATH at read-only folders, and the
            # caches must survive from the networked fetch to the offline
            # build, so keep them inside the disposable work copy.
            "--env", "HOME=" + workdir + "/.home",
            "--env", "CARGO_HOME=" + workdir + "/.home/.cargo",
            "--env", "GOPATH=" + workdir + "/.home/go",
            "--env", "PIP_NO_CACHE_DIR=1",
            "--volume", str(root) + ":" + workdir + ":rw",
            "--workdir", workdir,
            self.image,
            *[str(item) for item in command],
        ]

    def run(self, command, cwd, timeout, network=False):
        container_name = "repo-repair-" + uuid.uuid4().hex
        self.scratch_dir(cwd).mkdir(exist_ok=True)
        if sys.platform == "linux":
            self.write_passwd_file(cwd)

        def cleanup():
            run_command(
                ["docker", "rm", "-f", container_name], cwd, 10)

        return run_command(
            self.command(
                command,
                cwd,
                network=network,
                container_name=container_name,
            ),
            cwd,
            timeout,
            on_timeout=cleanup,
        )


def make_backend(name, image=ct.DEFAULT_PYTHON_IMAGE):
    if name == "local":
        return LocalBackend()
    if name == "docker":
        return DockerBackend(image)
    raise ValueError("Unsupported execution backend: " + str(name))
