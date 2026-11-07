"""Main code repair loop."""

import difflib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time

from agents import (
    ModelAgents, ModelResponseError, basic_run_steps, summarize_token_usage,
)
from backends import DockerBackend, make_backend
from execution import run_command
from metrics import summarize_repair_rate
from repository import (
    IGNORED, TOOLCHAINS, command_planning_context, inspect_repository,
    snapshot, source_suffixes)
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


def validate_repository_symlinks(root):
    """Allow only relative links that resolve inside the copied repository."""
    root = Path(root).resolve()
    for path in root.rglob("*"):
        if not path.is_symlink():
            continue
        relative = path.relative_to(root)
        link_target = Path(os.readlink(path))
        if link_target.is_absolute():
            raise ValueError(
                f"Unsupported symlink '{relative}': absolute target")
        try:
            resolved = path.resolve(strict=True)
            target_relative = resolved.relative_to(root)
        except FileNotFoundError:
            raise ValueError(
                f"Unsupported symlink '{relative}': dangling target") from None
        except ValueError:
            raise ValueError(
                f"Unsupported symlink '{relative}': target escapes repository"
            ) from None
        if IGNORED.intersection(target_relative.parts):
            raise ValueError(
                f"Unsupported symlink '{relative}': target is ignored")


def has_mixed_cosmetic_changes(before, after):
    """Return whether a functional patch also changes only whitespace."""
    cosmetic = False
    substantive = False
    for name, text in after.items():
        before_lines = before.get(name, "").splitlines(keepends=True)
        after_lines = text.splitlines(keepends=True)
        matcher = difflib.SequenceMatcher(None, before_lines, after_lines)
        for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if tag == "equal":
                continue
            old_changed = before_lines[old_start:old_end]
            new_changed = after_lines[new_start:new_end]
            old_blank = sum(not line.strip() for line in old_changed)
            new_blank = sum(not line.strip() for line in new_changed)
            old_content = [
                "".join(line.split()) for line in old_changed if line.strip()
            ]
            new_content = [
                "".join(line.split()) for line in new_changed if line.strip()
            ]
            if old_blank != new_blank:
                cosmetic = True
            if old_content != new_content:
                substantive = True
            elif old_changed != new_changed:
                cosmetic = True
    return cosmetic and substantive


def preserve_cosmetic_side_changes(before, after):
    """Restore separable whitespace-only edits in a functional patch."""
    if not has_mixed_cosmetic_changes(before, after):
        return after

    preserved = {}
    for name, text in after.items():
        before_lines = before.get(name, "").splitlines(keepends=True)
        after_lines = text.splitlines(keepends=True)
        matcher = difflib.SequenceMatcher(None, before_lines, after_lines)
        output = []
        for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            old_changed = before_lines[old_start:old_end]
            new_changed = after_lines[new_start:new_end]
            if tag == "equal":
                output.extend(old_changed)
            elif ("".join(old_changed).split()
                  == "".join(new_changed).split()):
                output.extend(old_changed)
            else:
                old_content = [
                    line for line in old_changed if line.strip()
                ]
                new_content = [
                    line for line in new_changed if line.strip()
                ]
                if len(old_content) == len(new_content):
                    replacements = iter(new_content)
                    for old_line in old_changed:
                        if not old_line.strip():
                            output.append(old_line)
                            continue
                        new_line = next(replacements)
                        if ("".join(old_line.split())
                                == "".join(new_line.split())):
                            output.append(old_line)
                        else:
                            output.append(new_line)
                else:
                    output.extend(new_changed)
        preserved[name] = "".join(output)
    return preserved


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
        timeout=120,
        allow_install=True,
        backend="auto",
        docker_image=None,
        allow_docker_pull=False,
        progress=None):
    run_start = time.monotonic()
    report = _repair(
        source,
        agents,
        rounds,
        timeout,
        allow_install,
        backend,
        docker_image,
        allow_docker_pull,
        progress,
    )
    requests = agents.requests if isinstance(agents, ModelAgents) else []
    report["metrics"] = {
        "wall_clock_seconds": round(time.monotonic() - run_start, 2),
        "model_api_seconds": round(
            agents.api_time_used_seconds
            if isinstance(agents, ModelAgents) else 0.0,
            2,
        ),
        "model_request_count": len(requests),
        "token_usage": summarize_token_usage(requests),
        "token_usage_by_agent": {
            agent: summarize_token_usage([
                request for request in requests
                if request.get("agent") == agent
            ])
            for agent in ("fixer", "judge")
        },
    }
    report["metrics"]["correct_repair_rate"] = summarize_repair_rate([report])
    return report


def _repair(
        source,
        agents,
        rounds=5,
        timeout=120,
        allow_install=True,
        backend="auto",
        docker_image=None,
        allow_docker_pull=False,
        progress=None):
    # Stop bad limits before any work starts.
    if rounds < 1 or timeout <= 0:
        raise ValueError("Rounds and timeout must be positive")
    source = Path(source).resolve()
    report = {
        "status": "failed",
        "reason": "Round limit reached",
        "pipeline": getattr(agents, "baseline_name", "proposed"),
        "history": [],
        "diff": "",
        "fixed_files": {},
    }
    stakeholder_baseline = (
        getattr(agents, "baseline_name", None) == "stakeholder")
    fixer_only_baseline = (
        getattr(agents, "baseline_name", None) == "fixer-only")

    def announce(message):
        if progress is not None:
            progress(message)

    # Add model call data only for live model runs.
    if isinstance(agents, ModelAgents):
        report["model_requests"] = agents.requests
        if agents.capture_logs:
            report["agent_logs"] = agents.logs

    # Keep the disposable work copy beside the selected repo. Docker Desktop
    # shares project folders but does not expose macOS system temp folders.
    with tempfile.TemporaryDirectory(
            prefix=".repo-repair-", dir=source.parent) as temporary:
        root = Path(temporary) / "repo"
        announce("\nPreparing temporary work copy...")

        # Preserve links without reading their targets, then validate the copy.
        shutil.copytree(
            source, root, symlinks=True,
            ignore=shutil.ignore_patterns(*IGNORED))
        validate_repository_symlinks(root)
        metadata = inspect_repository(root)
        report["repository"] = metadata

        execution_backend = select_backend(metadata, backend, docker_image)
        report["execution_backend"] = execution_backend.name

        def prepare_docker(candidate):
            # Check Docker and its image before any container command.
            announce("Checking Docker image: " + candidate.image)
            report["docker"] = {"image": candidate.image}
            availability = candidate.availability_status(root, timeout)
            report["docker"]["availability"] = availability
            if not command_passed(availability):
                report["reason"] = (
                    "Docker is unavailable. Install and start Docker, then retry.")
                return False

            image_status = candidate.image_status(root, timeout)
            report["docker"]["image_status"] = image_status
            if not command_passed(image_status):
                if not allow_docker_pull:
                    report["docker"]["pull_required"] = True
                    report["reason"] = (
                        "Docker image '" + candidate.image
                        + "' is not available locally. Re-run with "
                        "--allow-docker-pull to approve downloading it.")
                    return False
                announce("Pulling Docker image: " + candidate.image)
                pulled = candidate.pull(root, timeout)
                report["docker"]["pull"] = pulled
                if not command_passed(pulled):
                    report["reason"] = (
                        "Docker image '" + candidate.image
                        + "' could not be downloaded.")
                    return False
            return True

        if (isinstance(execution_backend, DockerBackend)
                and not prepare_docker(execution_backend)):
            return report

        if metadata["build_system"] == "unknown":
            report["reason"] = (
                "No supported build system found (expected one of: "
                + ", ".join(sorted(TOOLCHAINS)) + ")")
            return report
        toolchain = TOOLCHAINS[metadata["build_system"]]
        is_python = metadata["language"] == "python"
        readme_commands = (
            not is_python
            and isinstance(agents, ModelAgents)
            and not getattr(agents, "baseline_name", None)
        )
        if readme_commands:
            readme = next(
                (root / name for name in (
                    "README.md", "readme.md", "README.rst", "README.txt")
                 if (root / name).is_file()),
                None,
            )
            if readme is None:
                report["reason"] = (
                    "Non-Python repository has no README for command selection")
                return report
            announce("Planning repository commands from README...")
            try:
                command_plan = agents.plan_commands(
                    command_planning_context(root, readme))
            except Exception as error:
                report["reason"] = (
                    str(error)
                    if isinstance(error, ModelResponseError)
                    else "Judge returned invalid repository commands")
                return report
            metadata.update(command_plan)
            report["command_plan"] = command_plan

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

        def docker_mount_available():
            # Docker Desktop can silently mount an empty folder when the host
            # path is not shared. Detect that before running the repair loop.
            mount_check = execute(
                ["test", "-f", metadata["entry"]], root, timeout)
            report["docker"]["mount_check"] = mount_check
            if not command_passed(mount_check):
                report["reason"] = (
                    "Docker cannot access the temporary work copy. Share the "
                    "repository folder with Docker Desktop, then retry.")
                return False
            return True

        if (execution_backend.name == "docker"
                and not docker_mount_available()):
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
                    "stakeholder_baseline"
                    if stakeholder_baseline
                    else "fixer_only_baseline"
                    if fixer_only_baseline
                    else "fixer_model"
                    if isinstance(agents, ModelAgents)
                    else "offline"
                )
            except Exception:
                # A documentation failure must not undo a working repair.
                report["steps_to_run"] = basic_run_steps(steps_context)
                report["steps_source"] = "fallback"

        # Use Docker only when auto mode cannot find the requested Python.
        python_requirement = metadata.get("python_requirement")
        if (is_python and execution_backend.name == "local"
                and python_requirement):
            lookup = execute(
                [
                    "uv", "python", "find", "--no-project",
                    "--no-python-downloads", python_requirement,
                ],
                source,
                timeout,
            )
            report["python_lookup"] = lookup
            if not command_passed(lookup):
                if backend != "auto":
                    report["reason"] = (
                        "Required Python '" + python_requirement
                        + "' is not available locally.")
                    add_run_steps()
                    return report
                execution_backend = make_backend(
                    "docker", docker_image or metadata["docker_image"])
                report["execution_backend"] = "docker"
                report["docker_fallback"] = (
                    "Required Python '" + python_requirement
                    + "' is not available locally.")
                if (not prepare_docker(execution_backend)
                        or not docker_mount_available()):
                    add_run_steps()
                    return report

        # Keep local Python setup in the selected repo. Docker uses its copy.
        python = None
        announce(
            "Preparing Python environment..."
            if is_python else "Preparing build environment...")
        if not is_python:
            # Compiled toolchains need the cache folder named in backends.py.
            setup = execute(["mkdir", "-p", ".home/tmp"], root, timeout)
        elif execution_backend.name == "docker":
            python = "/workspace/.venv/bin/python"
            setup = execute(
                ["python", "-m", "venv", ".venv"], root, timeout)
        else:
            env = source / ".venv"
            env_python = env / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python")
            python = str(env_python)
            setup = execute(
                [
                    "uv", "--no-cache", "venv", "--no-project",
                    "--python", python_requirement or sys.executable,
                    "--no-python-downloads",
                    "--clear", str(env),
                ],
                source,
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

        fetch = (
            pip_install("-r") + ["requirements.txt"] if has_requirements
            else metadata.get("dependency_command", toolchain.get("fetch")))

        def verify():
            if readme_commands and allow_install and fetch:
                announce("Fetching declared dependencies...")
                before_manifests = read_manifests()
                setup = execute(fetch, root, timeout, network=True)
                keep_manifest_changes(before_manifests)
                report["setup"] = setup
                if not command_passed(setup):
                    return {"app": setup, "tests": None}
            # Run the app (or build) first, then run tests when they exist.
            announce(
                "Running application...\n" if is_python else "Running build...\n")
            app = execute(
                [python, metadata["entry"]] if is_python
                else metadata["build_command"], root, timeout)
            tests = None
            if metadata["test_command"]:
                announce("Running tests...\n")
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

        # Baselines and offline scripts retain their original dependency step.
        if not readme_commands and allow_install and fetch:
            announce("Fetching declared dependencies...")
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
        if passed(observation):
            report["final"] = observation
            report["no_repair_needed"] = True
            if observation["tests"] is not None:
                report["status"] = "passed"
                report["reason"] = "No errors found; no repair was needed"
            else:
                report["status"] = "execution_only"
                report["reason"] = (
                    "No execution error found; no repair was needed "
                    "(no tests detected)")
            return report
        seen = set()
        install_supported = (
            allow_install and (is_python or "add" in toolchain))
        if not allow_install:
            install_unavailable_reason = (
                "Dependency installation disabled by --no-install")
        elif not is_python and "add" not in toolchain:
            install_unavailable_reason = (
                "Dependency installation unsupported for "
                + metadata["build_system"])
        else:
            install_unavailable_reason = None
        capabilities = {
            "dependency_installation": {
                "supported": install_supported,
                "method": (
                    "pip" if is_python
                    else " ".join(toolchain["add"])
                    if "add" in toolchain else None
                ),
                "unavailable_reason": install_unavailable_reason,
            },
        }

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
                    "capabilities": capabilities,
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
                if not stakeholder_baseline and signature in seen:
                    record["error"] = "Repeated action rejected"
                    report["reason"] = (
                        "Fixer repeated a previously rejected action")
                    break
                seen.add(signature)

                # A report stops the run without calling the edit Judge.
                if action["action"] == "give_up":
                    report["reason"] = action["reason"]
                    break
                # The Judge reviews code edits, not package installs.
                if action["action"] == "install":
                    record["approval_required"] = False
                    if not install_supported:
                        record["error"] = install_unavailable_reason
                        report["reason"] = install_unavailable_reason
                        break
                    announce(
                        "Installing dependency proposed by Fixer: "
                        + action["package"])
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
                    if (command_passed(installation) and is_python
                            and not stakeholder_baseline):
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
                    files = (
                        action["files"]
                        if stakeholder_baseline
                        else preserve_cosmetic_side_changes(
                            before, action["files"])
                    )
                    cosmetic_changes_removed = files != action["files"]
                    proposed_diff = diff_files(before, files)
                    mixed_cosmetic = (
                        False
                        if stakeholder_baseline
                        else has_mixed_cosmetic_changes(before, files)
                    )
                    if fixer_only_baseline:
                        review = {"approved": True}
                        record["approval_required"] = False
                    else:
                        review = agents.review({
                            "before": before,
                            "after": files,
                            "diff": proposed_diff,
                            "reason": action["reason"],
                            "policy_findings": {
                                "cosmetic_side_changes_removed": (
                                    cosmetic_changes_removed),
                                "mixed_cosmetic_changes": mixed_cosmetic,
                            },
                        })
                        record["judge"] = review
                    if cosmetic_changes_removed:
                        record["cosmetic_side_changes_removed"] = True
                    if mixed_cosmetic:
                        record["error"] = "Patch includes cosmetic side changes"
                        report["reason"] = record["error"]
                        continue
                    if not review["approved"]:
                        continue
                    # Save the first file text for the final patch.
                    for name, text in files.items():
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
                if regressed and not stakeholder_baseline:
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
            if observation["tests"] is not None:
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
