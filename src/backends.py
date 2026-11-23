"""Execution backends for trusted local runs and isolated Docker runs."""

import os
from pathlib import Path
import sys
import time
import uuid

import constants as ct
from execution import run_command


class LocalBackend:
    """Run a command directly on the host."""

    name = "local"

    def run(self, command, cwd, timeout, network=False, user=None):
        del network, user
        return run_command(command, cwd, timeout)

    def close(self):
        """Nothing to clean up on the host."""


def host_cpus():
    """Give builds every host CPU; large repositories need them."""
    return os.cpu_count() or 2


def host_memory_limit(share=1):
    """Allow three quarters of host memory, with a 2g floor.

    share divides the amount, for a part that must stay smaller.
    """
    try:
        total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return "2g"
    return str(max(2, total * 3 // 4 // share // 2**30)) + "g"


# Run before every sandbox command: find the tools that the agents installed
# for the build user, then load the variables that the agents saved. A file
# with an error is tried in a child shell first and is not loaded, so a
# wrong line cannot stop every later command, including the one that fixes
# the file.
PRELUDE = (
    'export PATH="$REPAIR_HOME/.cargo/bin:$REPAIR_HOME/go/bin:'
    '$REPAIR_HOME/.local/bin:$PATH"; '
    'if [ -f "$REPAIR_HOME/env" ]; then '
    'if sh -c \'. "$REPAIR_HOME/env"\' >/dev/null 2>&1; then '
    '. "$REPAIR_HOME/env"; else '
    'echo "sandbox: $REPAIR_HOME/env has an error and was not loaded; '
    'correct or remove it" >&2; fi; fi; exec "$@"')

# Run once as root after the container starts. Programs and tests look up
# the current user, so the build user needs a passwd entry. rustup installs
# the toolchain that a repository pins, so its folders must be writable.
SETUP = (
    # The file for saved variables exists from the start, so a command
    # that edits it in place does not fail.
    'mkdir -p "$REPAIR_HOME" && touch "$REPAIR_HOME/env" && '
    'chown {user} "$REPAIR_HOME" "$REPAIR_HOME/env"; '
    'grep -q "^[^:]*:[^:]*:{uid}:" /etc/passwd || '
    'echo "repair:x:{uid}:{gid}:repair:$REPAIR_HOME:/bin/sh" >> /etc/passwd; '
    'r="${{RUSTUP_HOME:-/usr/local/rustup}}"; '
    'for d in "$r" "$r/toolchains" "$r/update-hashes" "$r/tmp" '
    '"$r/downloads"; do [ -d "$d" ] && chmod a+rwx "$d"; done; '
    '[ -f "$r/settings.toml" ] && chmod a+rw "$r/settings.toml"; true')


class DockerBackend:
    """Run every command of one repair in one disposable container.

    The container holds only the temporary work copy, never host
    credentials. It stays alive for the whole repair, so the system
    packages and tools that the agents install stay available.
    """

    name = "docker"
    # The home folder of the build user. It is outside the repository, so
    # dependency caches never show up in a project's own file scans.
    HOME = "/home/repair"
    # A memory file system. Some tests depend on how the disk file system
    # of the host reports file sizes or links; the agents can point such
    # tests here, for example with TMPDIR.
    MEMORY_DISK = "/ramdisk"

    def __init__(self, image=ct.DEFAULT_PYTHON_IMAGE):
        self.image = image
        self.container = None
        self.cwd = None

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

    def container_root(self, cwd):
        # Keep the repository's own folder name: some projects check it.
        return "/workspace/" + Path(cwd).resolve().name

    def home_dir(self, cwd):
        """Return the host folder mounted as the build user's home."""
        return Path(cwd).resolve().parent / ".sandbox-home"

    def build_user(self):
        # Docker Desktop maps some macOS temporary folders to container
        # root, so a host UID cannot write the work copy there. On a Linux
        # engine the files must stay owned by the host user.
        return (
            f"{os.getuid()}:{os.getgid()}" if sys.platform == "linux"
            else "0:0")

    def start_command(self, cwd, container_name):
        """Build the auditable docker command that starts the container."""
        workdir = self.container_root(cwd)
        home = self.HOME
        return [
            "docker", "run", "--detach", "--init", "--name", container_name,
            # Builds and tests download modules, fixtures, and toolchains.
            "--network", "bridge",
            "--cpus", str(host_cpus()),
            "--memory", host_memory_limit(),
            "--pids-limit", "16384",
            "--security-opt", "no-new-privileges",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "PYTHONUNBUFFERED=1",
            "--env", "PIP_NO_CACHE_DIR=1",
            "--env", "DEBIAN_FRONTEND=noninteractive",
            # The image points CARGO_HOME/GOPATH at folders that the build
            # user cannot write, so keep every cache in the mounted home.
            "--env", "REPAIR_HOME=" + home,
            "--env", "HOME=" + home,
            "--env", "CARGO_HOME=" + home + "/.cargo",
            "--env", "GOPATH=" + home + "/go",
            "--volume", str(Path(cwd).resolve()) + ":" + workdir + ":rw",
            "--volume", str(self.home_dir(cwd)) + ":" + home + ":rw",
            "--tmpfs", self.MEMORY_DISK + ":rw,exec,mode=1777,size="
            + host_memory_limit(4),
            "--workdir", workdir,
            self.image,
            "sleep", "infinity",
        ]

    def exec_command(self, command, cwd, timeout, user=None):
        """Build the auditable docker command used for one target command.

        user is None for the build user, "root" for environment setup, or
        "reader" for a user that cannot write the work copy.
        """
        docker_command = ["docker", "exec"]
        if user == "root":
            # Root keeps its own caches so the build user's stay writable.
            docker_command.extend([
                "--user", "0:0", "--env", "HOME=/root",
                "--env", "CARGO_HOME=/root/.cargo",
                "--env", "GOPATH=/root/go"])
        elif user == "reader":
            docker_command.extend([
                "--user", "65534:65534", "--env", "HOME=/tmp"])
        else:
            docker_command.extend(["--user", self.build_user()])
        return docker_command + [
            # One item, so reports keep the container path that the agents
            # can use in their own commands.
            "--workdir=" + self.container_root(cwd),
            self.container,
            # The limit must stop the processes inside the container;
            # stopping only the docker client would leave them running.
            "timeout", "-k", "10", str(int(timeout)),
            "sh", "-c", PRELUDE, "sh",
            *[str(item) for item in command],
        ]

    def start(self, cwd, timeout):
        """Start the container that serves every command of this repair."""
        name = "repo-repair-" + uuid.uuid4().hex
        # Docker would create a missing folder as root.
        self.home_dir(cwd).mkdir(exist_ok=True)
        started = run_command(self.start_command(cwd, name), cwd, timeout)
        if started["exit_code"] != 0 or started["timeout"]:
            run_command(["docker", "rm", "-f", name], cwd, 60)
            return started
        self.container = name
        self.cwd = cwd
        uid, gid = (os.getuid(), os.getgid()) if os.name == "posix" else (0, 0)
        run_command(
            self.exec_command(
                ["sh", "-c", SETUP.format(
                    user=self.build_user(), uid=uid, gid=gid)],
                cwd, 120, user="root"),
            cwd, 180)
        return started

    def restart(self):
        """Stop every process in the container but keep its files."""
        run_command(
            ["docker", "restart", "--time", "1", self.container],
            self.cwd, 120)

    def run(self, command, cwd, timeout, network=False, user=None):
        del network  # The container always has network access.
        if self.container is None:
            started = self.start(cwd, timeout)
            if self.container is None:
                return started
        start = time.monotonic()
        result = run_command(
            self.exec_command(command, cwd, timeout, user),
            cwd,
            timeout + 60,
            on_timeout=self.restart,
        )
        # Show the target command, not the docker wrapper around it.
        result["command"] = [str(item) for item in command]
        if (result["exit_code"] in (124, 137)
                and time.monotonic() - start >= timeout):
            result["timeout"] = True
        if user == "root" and sys.platform == "linux":
            # Root may leave files in the work copy that the build user
            # must be able to change and the host user must delete.
            run_command(
                self.exec_command(
                    ["find", self.container_root(cwd), self.HOME,
                     "-xdev", "-user", "0",
                     "-exec", "chown", "-h", self.build_user(), "{}", "+"],
                    cwd, 600, user="root"),
                cwd, 660)
        return result

    def close(self):
        """Remove the container at the end of the repair."""
        if self.container is None:
            return
        run_command(["docker", "rm", "-f", self.container], self.cwd, 120)
        self.container = None


def make_backend(name, image=ct.DEFAULT_PYTHON_IMAGE):
    if name == "local":
        return LocalBackend()
    if name == "docker":
        return DockerBackend(image)
    raise ValueError("Unsupported execution backend: " + str(name))
