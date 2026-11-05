"""Main code repair loop."""

import difflib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

from agents import ModelAgents, ModelResponseError, basic_run_steps
from backends import DockerBackend, make_backend
from execution import run_command
from repository import (
    IGNORED, TOOLCHAINS, inspect_repository, snapshot, source_suffixes)
from validation import validate_action


def diff_files(before, after):
    # Show the lines changed between the old and new files.
    return "".join(
        "".join(difflib.unified_diff(
            before.get(name, "").splitlines(keepends=True),
            after[name].splitlines(keepends=True),
            fromfile="a/" + name if name in before else "/dev/null",
            tofile="b/" + name,
        ))
        for name in sorted(after)
        if before.get(name) != after[name]
    )


def add_requirement(text, package):
    # Do not add a package that is already listed.
    wanted = re.sub(r"[-_.]+", "-", package).lower()
    for line in text.splitlines():
        match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9_.-]*)", line)
        if match:
            found = re.sub(r"[-_.]+", "-", match.group(1)).lower()
            if found == wanted:
                return text

    # Keep old text and add one direct dependency per line.
    if text and not text.endswith("\n"):
        text += "\n"
    return text + package + "\n"


def command_passed(result):
    """Return whether one recorded command completed successfully."""
    return result["exit_code"] == 0 and not result["timeout"]


def select_backend(metadata, requested, docker_image=None):
    """Choose local execution unless Docker is requested or required."""
    if requested not in {"auto", "local", "docker"}:
        raise ValueError("Unsupported execution backend: " + str(requested))
    # Compiled toolchains are only guaranteed inside their Docker images.
    if requested == "auto":
        requested = (
            "docker"
            if metadata["requires_linux"] or metadata["language"] != "python"
            else "local")
    image = docker_image or metadata["docker_image"]
    return make_backend(requested, image)


def repair(
        source,
        agents,
        rounds=5,
        timeout=30,
        allow_install=True,
        backend="auto",
        docker_image=None,
        allow_docker_pull=False):
    # Stop bad limits before any work starts.
    if rounds < 1 or timeout <= 0:
        raise ValueError("Rounds and timeout must be positive")
    source = Path(source).resolve()
    report = {
        "status": "failed",
        "reason": "Round limit reached",
        "history": [],
        "diff": "",
        "fixed_files": {},
    }
    # Add model call data only for live model runs.
    if isinstance(agents, ModelAgents):
        report["model_requests"] = agents.requests

    # Keep the disposable work copy beside the selected repo. Docker Desktop
    # shares project folders but does not expose macOS system temp folders.
    with tempfile.TemporaryDirectory(
            prefix=".repo-repair-", dir=source.parent) as temporary:
        root = Path(temporary) / "repo"

        # Links may point outside the work copy, so do not allow them.
        if any(path.is_symlink() for path in source.rglob("*")
               if not IGNORED.intersection(path.relative_to(source).parts)):
            raise ValueError("Symlinks are unsupported")
        # Copy the repo so the source files stay unchanged.
        shutil.copytree(source, root, ignore=shutil.ignore_patterns(*IGNORED))
        metadata = inspect_repository(root)
        report["repository"] = metadata

        execution_backend = select_backend(metadata, backend, docker_image)
        report["execution_backend"] = execution_backend.name
        if isinstance(execution_backend, DockerBackend):
            report["docker"] = {"image": execution_backend.image}
            availability = execution_backend.availability_status(root, timeout)
            report["docker"]["availability"] = availability
            if not command_passed(availability):
                report["reason"] = (
                    "Docker is unavailable. Install and start Docker, then retry.")
                return report

            image_status = execution_backend.image_status(root, timeout)
            report["docker"]["image_status"] = image_status
            if not command_passed(image_status):
                if not allow_docker_pull:
                    report["docker"]["pull_required"] = True
                    report["reason"] = (
                        "Docker image '" + execution_backend.image
                        + "' is not available locally. Re-run with "
                        "--allow-docker-pull to approve downloading it.")
                    return report
                pulled = execution_backend.pull(root, timeout)
                report["docker"]["pull"] = pulled
                if not command_passed(pulled):
                    report["reason"] = (
                        "Docker image '" + execution_backend.image
                        + "' could not be downloaded.")
                    return report

        if metadata["build_system"] == "unknown":
            report["reason"] = (
                "No supported build system found (expected one of: "
                + ", ".join(sorted(TOOLCHAINS)) + ")")
            return report
        toolchain = TOOLCHAINS[metadata["build_system"]]
        is_python = metadata["language"] == "python"

        def execute(command, cwd, command_timeout, network=False):
            # Keep local execution patchable by the existing test suite.
            if execution_backend.name == "local":
                return run_command(command, cwd, command_timeout)
            return execution_backend.run(
                command,
                cwd,
                command_timeout,
                network=network,
            )

        if execution_backend.name == "docker":
            # Docker Desktop can silently mount an empty folder when the host
            # path is not shared. Detect that before running the repair loop.
            mount_check = execute(
                ["test", "-f", metadata["entry"]], root, timeout)
            report["docker"]["mount_check"] = mount_check
            if not command_passed(mount_check):
                report["reason"] = (
                    "Docker cannot access the temporary work copy. Share the "
                    "repository folder with Docker Desktop, then retry.")
                return report

        installed_packages = []
        has_requirements = is_python and (root / "requirements.txt").is_file()
        # Record manifest text so tool-made dependency edits reach the patch.
        manifests = toolchain.get("manifests", [])
        originals = {}

        def read_manifests():
            return {
                name: (root / name).read_text()
                for name in manifests if (root / name).is_file()
            }

        def keep_manifest_changes(before):
            after = read_manifests()
            for name in set(before) | set(after):
                if before.get(name, "") != after.get(name, ""):
                    originals.setdefault(name, before.get(name, ""))

        def add_run_steps():
            # Save AI instructions, with a simple backup if that call fails.
            steps_context = {
                "repository": metadata,
                "has_requirements": has_requirements,
                "installed_packages": sorted(set(installed_packages)),
                "fixed_files": sorted(report["fixed_files"]),
                "status": report["status"],
            }
            try:
                steps = agents.generate_run_steps(steps_context)
                lower_steps = steps.lower()
                if has_requirements and "requirements.txt" not in lower_steps:
                    raise ValueError("Run steps omit requirements.txt")
                if report["fixed_files"]:
                    copy_at = lower_steps.find("fixed_files")
                    install_at = lower_steps.find("install")
                    if copy_at < 0 or (install_at >= 0 and copy_at > install_at):
                        raise ValueError("Run steps copy repaired files too late")
                if any(
                        package.lower() not in lower_steps
                        for package in installed_packages):
                    raise ValueError("Run steps omit an installed package")
                report["steps_to_run"] = steps
                report["steps_source"] = (
                    "fixer_model"
                    if isinstance(agents, ModelAgents)
                    else "offline")
            except Exception:
                # A documentation failure must not undo a working repair.
                report["steps_to_run"] = basic_run_steps(steps_context)
                report["steps_source"] = "fallback"

        # Start clean so old packages cannot hide missing dependencies. Docker
        # writes its environment inside the temporary work copy, never source.
        python = None
        if not is_python:
            # Compiled toolchains need the cache folder named in backends.py.
            setup = execute(["mkdir", "-p", ".home/tmp"], root, timeout)
        elif execution_backend.name == "docker":
            python = "/workspace/.venv/bin/python"
            setup = execute(
                ["python", "-m", "venv", ".venv"], root, timeout)
        else:
            env = root / ".venv"
            env_python = env / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python")
            python = str(env_python)
            setup = execute(
                [
                    "uv", "--no-cache", "venv", "--no-project",
                    "--python", sys.executable, "--no-python-downloads",
                    "--clear", str(env),
                ],
                root,
                timeout,
            )
        report["environment_setup"] = setup
        if setup["exit_code"] != 0 or setup["timeout"]:
            report["reason"] = (
                "Docker environment setup failed"
                if execution_backend.name == "docker"
                else "Python environment setup failed")
            add_run_steps()
            return report

        def with_python(command):
            return [python] + command if python else command

        def pip_install(package):
            return (
                ["uv", "pip", "install", "--python", python, package]
                if execution_backend.name == "local"
                else [python, "-m", "pip", "install", package])

        def verify():
            # Run the app (or build) first, then run tests when they exist.
            app = execute(
                [python, metadata["entry"]] if is_python
                else metadata["build_command"], root, timeout)
            tests = None
            if metadata["test_command"]:
                tests = execute(
                    with_python(metadata["test_command"]), root, timeout)
            return {"app": app, "tests": tests}

        def passed(observation):
            # Every command must finish on time with exit code zero.
            return all(
                not item["timeout"] and item["exit_code"] == 0
                for item in observation.values()
                if item is not None
            )

        # Install listed packages only when the user allows it. Builds then
        # run with the network off, so declared dependencies are fetched here.
        fetch = (
            pip_install("-r") + ["requirements.txt"] if has_requirements
            else toolchain.get("fetch"))
        if allow_install and fetch:
            before_manifests = read_manifests()
            setup = execute(fetch, root, timeout, network=True)
            keep_manifest_changes(before_manifests)
            report["setup"] = setup
            if setup["exit_code"] != 0 or setup["timeout"]:
                report["reason"] = "Declared dependency installation failed"
                add_run_steps()
                return report

        # Save the first app and test result before asking for a fix.
        observation = verify()
        report["initial"] = observation
        seen = set()

        for _ in range(rounds):
            # Stop as soon as both the app and tests pass.
            if passed(observation):
                break
            record = {}
            report["history"].append(record)
            try:
                # Give the Fixer the repo files, errors, and past work.
                action = agents.propose({
                    "repository": metadata,
                    "files": snapshot(root, metadata["build_system"]),
                    "observation": observation,
                    "history": report["history"][:-1],
                })
                validate_action(
                    action, root, source_suffixes(metadata["build_system"]))
                record["action"] = action
                # Do not run the same action more than once.
                signature = json.dumps(
                    {key: value for key, value in action.items()
                     if key != "reason"},
                    sort_keys=True,
                )
                if signature in seen:
                    record["error"] = "Repeated action rejected"
                    continue
                seen.add(signature)

                # The Fixer can stop when it cannot find a safe fix.
                if action["action"] == "give_up":
                    report["reason"] = action["reason"]
                    break
                # Package installs do not change source files.
                if action["action"] == "install":
                    if not allow_install:
                        record["error"] = "Dependency installation disabled"
                        continue
                    if not is_python and "add" not in toolchain:
                        record["error"] = (
                            "Dependency installation unsupported for "
                            + metadata["build_system"])
                        continue
                    review = agents.review_install({
                        "package": action["package"],
                        "reason": action["reason"],
                        "repository": metadata,
                        "observation": observation,
                    })
                    record["judge"] = review
                    if not review["approved"]:
                        continue
                    before_manifests = read_manifests()
                    installation = execute(
                        pip_install(action["package"]) if is_python
                        else toolchain["add"] + [action["package"]],
                        root,
                        timeout,
                        network=True,
                    )
                    record["installation"] = installation
                    # cargo add and go get edit the manifest themselves.
                    keep_manifest_changes(before_manifests)
                    if command_passed(installation):
                        installed_packages.append(action["package"])
                    if command_passed(installation) and is_python:
                        requirements = root / "requirements.txt"
                        old_requirements = (
                            requirements.read_text()
                            if requirements.is_file()
                            else "")
                        new_requirements = add_requirement(
                            old_requirements, action["package"])
                        if new_requirements != old_requirements:
                            originals.setdefault(
                                "requirements.txt", old_requirements)
                            requirements.write_text(new_requirements)
                            has_requirements = True
                            record["requirements_updated"] = True
                else:
                    # Send every code edit to the Judge before writing it.
                    before = {
                        name: (root / name).read_text()
                        for name in action["files"]
                    }
                    review = agents.review({
                        "before": before,
                        "after": action["files"],
                        "diff": diff_files(before, action["files"]),
                        "reason": action["reason"],
                    })
                    record["judge"] = review
                    if not review["approved"]:
                        continue
                    # Save the first file text for the final patch.
                    for name, text in action["files"].items():
                        originals.setdefault(name, before[name])
                        (root / name).write_text(text)

                # Run the app and tests again after the action.
                updated = verify()
                record["observation"] = updated
                regressed = action["action"] == "edit" and any(
                    observation[key] is not None
                    and observation[key]["exit_code"] == 0
                    and not observation[key]["timeout"]
                    and (updated[key]["exit_code"] != 0
                         or updated[key]["timeout"])
                    for key in observation
                )
                # Undo the edit if a command that passed now fails.
                if regressed:
                    for name, text in before.items():
                        (root / name).write_text(text)
                    record["rolled_back"] = True
                else:
                    observation = updated
            except Exception as error:
                # Save a safe error name without model reply text.
                record["error"] = type(error).__name__
                if isinstance(error, ModelResponseError):
                    report["reason"] = str(error)
                else:
                    report["reason"] = (
                        "Invalid action or infrastructure failure ("
                        + type(error).__name__ + ")")
                break

        # Set the final status and make the patch for the user.
        report["final"] = observation
        if passed(observation):
            if metadata["tests"]:
                report["status"] = "passed"
                report["reason"] = "Execution and detected tests passed"
            else:
                report["status"] = "execution_only"
                report["reason"] = (
                    "Execution passed; functionality not verified "
                    "(no tests detected)")
        final_files = {
            name: (root / name).read_text()
            for name in originals
        }
        report["diff"] = diff_files(originals, final_files)

        # Save changed file text only when the full repair passed.
        if report["status"] in {"passed", "execution_only"}:
            report["fixed_files"] = {
                name: text
                for name, text in final_files.items()
                if originals[name] != text
            }

        add_run_steps()
    return report
