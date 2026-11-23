"""Main code repair loop."""

import contextlib
import difflib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import urllib.error

from agents import (
    AgentTimeLimitError, ModelAgents, ModelResponseError, basic_run_steps,
    summarize_token_usage,
)
from backends import DockerBackend, make_backend
import constants as ct
from execution import run_command
from metrics import summarize_repair_rate
from repository import (
    IGNORED, TOOLCHAINS, changed_files, command_planning_context,
    file_manifest, inspect_repository, is_test_path, mentioned_files,
    read_repository_file, root_listing, search_repository, snapshot,
    source_suffixes)
from validation import (
    EXPLORATIONS, REMOTE_BUILD, SYSTEM_INSTALL, apply_replacements,
    validate_action)


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


def validate_repository_symlink(root, path, link_target):
    """Check one link target without changing the repository."""
    root = Path(root).resolve()
    path = Path(path)
    relative = path.relative_to(root)
    link_target = Path(link_target)
    if link_target.is_absolute():
        raise ValueError(
            f"Unsupported symlink '{relative}': absolute target")
    try:
        # A link to a file that the repository lacks is a fault that the
        # agents can repair. It must still point inside the repository.
        resolved = (path.parent / link_target).resolve()
        target_relative = resolved.relative_to(root)
    except (OSError, RuntimeError):
        raise ValueError(
            f"Unsupported symlink '{relative}': unreadable target") from None
    except ValueError:
        raise ValueError(
            f"Unsupported symlink '{relative}': target escapes repository"
        ) from None
    if IGNORED.intersection(target_relative.parts):
        raise ValueError(
            f"Unsupported symlink '{relative}': target is ignored")


def validate_repository_symlinks(root):
    """Allow only relative links that resolve inside the copied repository."""
    root = Path(root).resolve()
    for path in root.rglob("*"):
        if path.is_symlink():
            validate_repository_symlink(root, path, os.readlink(path))


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
            # Blank lines that come or go with added or removed code are
            # part of that code, not a separate cosmetic change.
            if old_blank != new_blank and len(old_content) == len(new_content):
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


def judge_history(history, outputs=False):
    """Keep the useful parts of earlier rounds for the Judge.

    The Fixer gets the same summary, with the end of each command output.
    """
    saved = []
    for round_number, record in enumerate(history, start=1):
        item = {"round": round_number}
        action = record.get("action")
        if isinstance(action, dict):
            item["action"] = action.get("action")
            item["reason"] = action.get("reason")
            if action.get("action") == "edit":
                item["files"] = sorted(
                    set(action.get("files", {}))
                    | set(action.get("replacements", {})))
            elif action.get("action") == "install":
                item["package"] = action.get("package")
            elif action.get("action") == "run":
                item["command"] = action.get("command")
                item["root"] = action.get("root", False)
            elif action.get("action") == "commands":
                item["build_command"] = action.get("build_command")
                item["test_command"] = action.get("test_command")

        review = record.get("judge")
        if isinstance(review, dict):
            item["judge"] = {
                "approved": review.get("approved"),
                "reason": review.get("reason"),
            }
        if record.get("error"):
            item["error"] = record["error"]
        if record.get("error_detail"):
            item["error_detail"] = record["error_detail"]
        for name in ("command_result", "installation"):
            result = record.get(name)
            if isinstance(result, dict):
                item[name] = {
                    "exit_code": result.get("exit_code"),
                    "timeout": result.get("timeout", False),
                }
                if outputs:
                    item[name]["output"] = output_tail(result)
        if record.get("changed_files"):
            item["changed_files"] = record["changed_files"]
        if record.get("rolled_back"):
            item["rolled_back"] = True

        observation = record.get("observation")
        if isinstance(observation, dict):
            item["result"] = {
                name: (
                    None if result is None else {
                        "exit_code": result.get("exit_code"),
                        "timeout": result.get("timeout", False),
                    }
                )
                for name, result in observation.items()
            }
        saved.append(item)
    return saved


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


def copy_ignore(source):
    """Skip caches in every folder and the saved offline script at the root.

    The script holds the expected repair, so the agents must not see it.
    """
    caches = shutil.ignore_patterns(*IGNORED)

    def ignore(folder, entries):
        skipped = set(caches(folder, entries))
        if Path(folder) == source and "repair.json" in entries:
            skipped.add("repair.json")
        return skipped
    return ignore


def agent_view(metadata):
    """Bound the repository facts that go into a prompt."""
    view = dict(metadata)
    tests = view.get("tests") or []
    if len(tests) > 20:
        view["tests"] = tests[:20]
        view["test_file_count"] = len(tests)
    return view


def output_tail(result, limit=ct.HISTORY_OUTPUT_CHARS):
    """Return the end of one command's output for a prompt."""
    text = (result.get("stdout", "") + "\n" + result.get("stderr", "")).strip()
    return text[-limit:]


def ran_no_tests(result):
    """Return whether a passing test command reports that it ran no tests."""
    text = result.get("stdout", "") + "\n" + result.get("stderr", "")
    if re.search(
            r"No tests were found|No tests defined|collected 0 items"
            r"|^Ran 0 tests", text, re.MULTILINE):
        return True
    counts = re.findall(r"^running (\d+) tests?$", text, re.MULTILINE)
    return bool(counts) and not any(int(count) for count in counts)


def failure_text(observation):
    """Return the failing output of one check, without numbers that vary."""
    return re.sub(r"\d+", "#", "\n".join(
        name + str(item["exit_code"]) + item.get("stdout", "")[-2000:]
        + item.get("stderr", "")[-2000:]
        for name, item in sorted(observation.items())
        if item is not None and (item["exit_code"] != 0 or item["timeout"])))


def evidence_excerpt(text, commands):
    """Return the lines of a document that mention the proposed commands."""
    words = {
        word for command in commands if command
        for word in re.findall(r"[A-Za-z0-9_./-]{4,}", command)}
    lines = text.splitlines()
    keep = set()
    for number, line in enumerate(lines):
        if any(word in line for word in words):
            keep.update(range(max(0, number - 2), min(len(lines), number + 3)))
    excerpt = "\n".join(lines[number] for number in sorted(keep))
    return (excerpt or text)[:6000]


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
            prefix=".repo-repair-", dir=source.parent) as temporary, (
            contextlib.ExitStack()) as cleanup:
        # Keep the repository's own folder name: some projects check it.
        root = Path(temporary) / source.name
        announce("\nPreparing temporary work copy...")

        # Preserve links without reading their targets, then validate the copy.
        shutil.copytree(
            source, root, symlinks=True, ignore=copy_ignore(source))
        validate_repository_symlinks(root)
        # The copy keeps file times, so this records the original state.
        manifest = file_manifest(root)
        protected = [name for name in manifest if is_test_path(name)]
        metadata = inspect_repository(root)
        report["repository"] = metadata

        execution_backend = select_backend(metadata, backend, docker_image)
        report["execution_backend"] = execution_backend.name
        # Remove the sandbox container before the work copy is deleted.
        cleanup.callback(lambda: execution_backend.close())

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
                local_image = candidate.dockerfile() is not None
                if not allow_docker_pull:
                    report["docker"]["pull_required"] = True
                    report["reason"] = (
                        "Docker image '" + candidate.image
                        + "' is not available locally. Re-run with "
                        "--allow-docker-pull to approve "
                        + ("building it." if local_image
                           else "downloading it."))
                    return False
                if local_image:
                    announce("Building Docker image: " + candidate.image)
                    pulled = candidate.build_image(root, max(timeout, 1800))
                else:
                    announce("Pulling Docker image: " + candidate.image)
                    pulled = candidate.pull(root, timeout)
                report["docker"]["pull"] = pulled
                if not command_passed(pulled):
                    report["reason"] = (
                        "Docker image '" + candidate.image
                        + "' could not be "
                        + ("built." if local_image else "downloaded."))
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
                    "README.md", "readme.md", "README.rst", "README.txt",
                    "README")
                 if (root / name).is_file()),
                None,
            )
            announce("Planning repository commands from README...")
            try:
                command_plan = agents.plan_commands(command_planning_context(
                    root, readme, metadata["build_system"]))
            except Exception as error:
                # The toolchain defaults build most repositories, and the
                # Fixer can propose better commands during the repair.
                command_plan = {}
                report["command_plan_error"] = (
                    str(error) if isinstance(error, ModelResponseError)
                    else type(error).__name__)
            else:
                # Detected tests must still run when the plan names none.
                if command_plan["test_command"] is None:
                    command_plan["test_command"] = metadata["test_command"]
                if REMOTE_BUILD.search(" ".join(command_plan["build_command"])):
                    # An install of a published copy does not build this
                    # repository; use the default build command.
                    command_plan["build_command"] = metadata["build_command"]
                    report["command_plan_note"] = (
                        "The planned build command fetched a published "
                        "copy, so the default build command is used")
                dependency = command_plan["dependency_command"]
                if dependency and SYSTEM_INSTALL.search(" ".join(dependency)):
                    command_plan["dependency_command"] = None
                    report["dependency_command_note"] = (
                        "The planned dependency command installed system "
                        "packages, which needs root, so it is not used")
                metadata.update(command_plan)
            report["command_plan"] = command_plan

        def execute(command, cwd, command_timeout, network=False, user=None):
            # Keep local execution patchable by the existing test suite.
            if execution_backend.name == "local":
                return run_command(command, cwd, command_timeout)
            return execution_backend.run(
                command,
                cwd,
                command_timeout,
                network=network,
                **({"user": user} if user else {}),
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
        environment_commands = []
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
                "repository": agent_view(metadata),
                "has_requirements": has_requirements,
                "installed_packages": sorted(set(installed_packages)),
                "environment_commands": environment_commands,
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
                if any(
                        item["command"] not in steps
                        for item in environment_commands):
                    raise ValueError("Run steps omit an environment command")
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
            # Check that the sandbox runs a command as the build user.
            setup = execute(["true"], root, timeout)
        elif execution_backend.name == "docker":
            python = execution_backend.container_root(root) + "/.venv/bin/python"
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

        def restore_tests():
            # No action or command may change the tests that prove a repair.
            restored = []
            for name in changed_files(root, manifest, protected):
                original = source / name
                if original.is_symlink() or not original.is_file():
                    continue
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, root / name)
                restored.append(name)
            return restored

        def verify():
            restored = restore_tests()
            if restored:
                report.setdefault("restored_test_files", []).extend(
                    name for name in restored[:50]
                    if name not in report["restored_test_files"])
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
            if metadata["test_command"] and (is_python or command_passed(app)):
                announce("Running tests...\n")
                started = time.time()
                tests = execute(
                    with_python(metadata["test_command"]), root, timeout)
                if (not command_passed(tests) and not tests["timeout"]
                        and not is_python
                        and time.time() - started < ct.TEST_RETRY_SECONDS):
                    # Run once more to separate a test that fails now and
                    # then from a real failure.
                    announce("Tests failed; running them once more...\n")
                    again = execute(
                        with_python(metadata["test_command"]), root, timeout)
                    if command_passed(again):
                        again["attempts"] = 2
                        report["tests_note"] = (
                            "The tests failed once and passed on the "
                            "second run")
                        tests = again
                if command_passed(tests) and ran_no_tests(tests):
                    # A command that ran no tests proves nothing; report
                    # the build only.
                    report["tests_note"] = (
                        "The test command ran no tests, so only the build "
                        "is verified")
                    tests = None
            elif metadata["test_command"]:
                # Tests of a compiled project cannot pass without its build.
                tests = {
                    "command": metadata["test_command"],
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": "Not run: the build failed",
                    "timeout": False,
                    "skipped": True,
                }
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
        # Each tried action, with the failure that it was tried against.
        seen = {}
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
        # Shell actions need the sandbox; they never run on the host.
        shell_supported = (
            execution_backend.name == "docker" and not stakeholder_baseline)
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
            "shell": {
                "supported": shell_supported,
                **({
                    "workdir": execution_backend.container_root(root),
                    "env_file": execution_backend.HOME + "/env",
                    "memory_disk": execution_backend.MEMORY_DISK,
                    "package_manager": "apt-get (root:true)",
                    "users": {
                        "build": "runs the build, the tests, and run "
                                 "actions; can write the repository",
                        "inspect": "runs inspect actions; can write only "
                                   "/tmp and memory_disk",
                    },
                } if shell_supported else {}),
            },
        }
        suffixes = source_suffixes(metadata["build_system"])
        explorations_left = rounds * ct.EXPLORATIONS_PER_ROUND
        explored = []
        report["explorations"] = 0
        model_failures = 0
        gave_up_before = False

        def reject(record, message, error="ValueError"):
            # Tell the Fixer what was wrong; the round is used.
            record["error"] = error
            record["error_detail"] = message
            report["reason"] = message

        def review(context):
            # The fixer-only baseline runs every proposal without a Judge.
            if fixer_only_baseline:
                return {"approved": True}
            return agents.review({
                "repository": agent_view(metadata),
                "observation": observation,
                "history": judge_history(report["history"][:-1]),
                # Only the sandbox details: the language package field
                # does not limit system packages, and it misled the Judge.
                "capabilities": {"shell": capabilities["shell"]},
                **context,
            })

        while len(report["history"]) < rounds and not passed(observation):
            failed_output = "\n".join(
                item.get("stdout", "") + "\n" + item.get("stderr", "")
                for item in observation.values() if item is not None)
            try:
                # Give the Fixer the repo files, errors, and past work.
                action = agents.propose({
                    "repository": agent_view(metadata),
                    "root_listing": root_listing(root),
                    # Send repository files only, never build output.
                    "files": snapshot(
                        root, metadata["build_system"], mentioned_files(
                            root, failed_output, metadata["build_system"]),
                        set(manifest) | set(originals)),
                    "observation": observation,
                    "history": (
                        report["history"] if stakeholder_baseline
                        else judge_history(report["history"], outputs=True)),
                    "explored": explored,
                    "capabilities": capabilities,
                    "budget": {
                        "rounds_left": rounds - len(report["history"]),
                        "explorations_left": explorations_left,
                    },
                })
            except Exception as error:
                # Save a safe error name without model reply text.
                report["history"].append({"error": type(error).__name__})
                report["reason"] = (
                    str(error) if isinstance(error, ModelResponseError)
                    else "Invalid action or infrastructure failure ("
                    + type(error).__name__ + ")")
                model_failures += 1
                if (isinstance(error, AgentTimeLimitError)
                        or model_failures >= 3):
                    break
                if isinstance(error, urllib.error.HTTPError):
                    # The model server is busy; give it time.
                    time.sleep(ct.MODEL_HTTP_RETRY_SECONDS)
                continue
            model_failures = 0

            try:
                validate_action(action, root, suffixes, shell=shell_supported)
            except ValueError as error:
                record = {}
                if isinstance(action, dict) and isinstance(
                        action.get("action"), str):
                    record["action"] = {
                        key: value for key, value in action.items()
                        if key in {"action", "reason", "path", "command",
                                   "package", "build_command", "test_command"}
                        and isinstance(value, str)}
                report["history"].append(record)
                reject(record, str(error))
                continue

            kind = action["action"]
            if kind in EXPLORATIONS:
                # Look without changing anything. No repair round is used
                # until the exploration limit is reached; then each look
                # uses one round.
                if explorations_left > 0:
                    explorations_left -= 1
                else:
                    report["history"].append(
                        {"action": action, "exploration": True})
                report["explorations"] += 1
                if kind == "read":
                    announce("Fixer reads: " + action["path"])
                    result = read_repository_file(
                        root, action["path"], action.get("start"),
                        action.get("end"))
                elif kind == "search":
                    announce("Fixer searches: " + action["pattern"])
                    result = "\n".join(search_repository(
                        root, action["pattern"],
                        action.get("path") or None)) or "No matches"
                else:
                    announce("Fixer inspects: " + action["command"])
                    inspected = execute(
                        ["sh", "-c", action["command"]], root,
                        min(timeout, 300), user="reader")
                    result = (
                        "exit code " + str(inspected["exit_code"]) + "\n"
                        + output_tail(inspected, ct.MAX_READ_CHARS))
                explored.append({
                    "action": {
                        key: value for key, value in action.items()
                        if key != "reason"},
                    "result": result,
                })
                # Drop the oldest results when the prompt would grow too big.
                while (len(explored) > 1 and sum(
                        len(item["result"]) for item in explored)
                        > ct.MAX_EXPLORED_CHARS):
                    explored.pop(0)
                continue

            record = {"action": action}
            report["history"].append(record)
            # Do not run the same action more than once.
            signature = json.dumps(
                {key: value for key, value in action.items()
                 if key != "reason"},
                sort_keys=True,
            )
            # The same action is permitted again after the failure changed:
            # the Judge can reject a correct action that came too early.
            state = failure_text(observation)
            if not stakeholder_baseline and seen.get(signature) == state:
                reject(
                    record,
                    "This action was already tried against this failure; "
                    "send a different action",
                    "Repeated action rejected")
                continue
            seen[signature] = state

            try:
                # A report stops the run without calling the edit Judge.
                if kind == "give_up":
                    if (shell_supported and not gave_up_before
                            and len(report["history"]) < rounds):
                        # The sandbox can still change; ask once more.
                        gave_up_before = True
                        seen.pop(signature, None)
                        reject(
                            record,
                            "Rounds are left. Explore with inspect, read, "
                            "or search, or prepare the environment with "
                            "run. Send give_up again only when no action "
                            "can make progress.",
                            "give_up declined once")
                        continue
                    report["reason"] = action["reason"]
                    break
                if kind == "install":
                    # The Judge reviews code edits, not package installs.
                    record["approval_required"] = False
                    if not install_supported:
                        reject(
                            record, install_unavailable_reason,
                            install_unavailable_reason)
                        continue
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
                elif kind == "run":
                    verdict = review({
                        "kind": "run",
                        "proposal": {
                            "command": action["command"],
                            "root": action.get("root", False),
                        },
                        "reason": action["reason"],
                    })
                    if fixer_only_baseline:
                        record["approval_required"] = False
                    else:
                        record["judge"] = verdict
                    if not verdict["approved"]:
                        continue
                    announce("Running command proposed by Fixer: "
                             + action["command"])
                    changed_before = set(changed_files(root, manifest))
                    command_result = execute(
                        ["sh", "-c", action["command"]], root, timeout,
                        network=True,
                        user="root" if action.get("root") else None)
                    record["command_result"] = command_result
                    # Show which original files the command changed.
                    changed = sorted(
                        set(changed_files(root, manifest)) - changed_before)
                    restored = restore_tests()
                    if restored:
                        record["restored_test_files"] = restored[:50]
                    changed = [
                        name for name in changed if name not in restored]
                    if changed:
                        record["changed_files"] = changed[:50]
                    for name in changed:
                        try:
                            originals.setdefault(
                                name, (source / name).read_text())
                        except (OSError, UnicodeDecodeError):
                            pass
                    if not command_passed(command_result):
                        continue
                    environment_commands.append({
                        "command": action["command"],
                        "root": action.get("root", False),
                    })
                    if action.get("verify") is False:
                        continue
                elif kind == "commands":
                    proposal = {
                        "build_command": action["build_command"],
                        "test_command": action.get("test_command"),
                    }
                    if is_python:
                        reject(record, "Python repositories run their entry "
                               "file and unittest; commands cannot change")
                        continue
                    if metadata["test_command"] and not proposal["test_command"]:
                        reject(record, "The test command cannot be removed")
                        continue
                    verdict = review({
                        "kind": "commands",
                        "current": {
                            "build_command": metadata["build_command"],
                            "test_command": metadata["test_command"],
                        },
                        "proposal": proposal,
                        "evidence": {
                            name: evidence_excerpt(
                                (root / name).read_text(errors="replace"),
                                proposal.values())
                            for name in action.get("evidence", [])
                        },
                        "reason": action["reason"],
                    })
                    if fixer_only_baseline:
                        record["approval_required"] = False
                    else:
                        record["judge"] = verdict
                    if not verdict["approved"]:
                        continue
                    announce("Using commands proposed by Fixer")
                    metadata["build_command"] = [
                        "sh", "-c", proposal["build_command"]]
                    metadata["test_command"] = (
                        ["sh", "-c", proposal["test_command"]]
                        if proposal["test_command"] else None)
                    # The new build command fetches what it needs.
                    metadata["dependency_command"] = None
                    fetch = None
                    report.setdefault("command_changes", []).append(proposal)
                else:
                    # Send every code edit to the Judge before writing it.
                    names = list(action.get("files", {})) + list(
                        action.get("replacements", {}))
                    created = [
                        name for name in names if not (root / name).exists()]
                    before = {
                        name: "" if name in created
                        else (root / name).read_text()
                        for name in names
                    }
                    proposed = dict(action.get("files", {}))
                    for name, changes in action.get(
                            "replacements", {}).items():
                        proposed[name] = apply_replacements(
                            before[name], changes, name)
                    files = (
                        proposed
                        if stakeholder_baseline
                        else preserve_cosmetic_side_changes(before, proposed)
                    )
                    cosmetic_changes_removed = files != proposed
                    proposed_diff = diff_files(before, files)
                    mixed_cosmetic = (
                        False
                        if stakeholder_baseline
                        else has_mixed_cosmetic_changes(before, files)
                    )
                    if fixer_only_baseline:
                        verdict = {"approved": True}
                        record["approval_required"] = False
                    else:
                        verdict = agents.review({
                            "repository": agent_view(metadata),
                            "observation": observation,
                            "history": judge_history(report["history"][:-1]),
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
                        record["judge"] = verdict
                    if cosmetic_changes_removed:
                        record["cosmetic_side_changes_removed"] = True
                    if mixed_cosmetic:
                        record["error"] = "Patch includes cosmetic side changes"
                        report["reason"] = record["error"]
                        continue
                    if not verdict["approved"]:
                        continue
                    # Save the first file text for the final patch.
                    for name, text in files.items():
                        originals.setdefault(name, before[name])
                        (root / name).parent.mkdir(parents=True, exist_ok=True)
                        (root / name).write_text(text)

                # Run the app and tests again after the action.
                updated = verify()
                record["observation"] = updated
                regressed = kind == "edit" and any(
                    observation[key] is not None
                    and observation[key]["exit_code"] == 0
                    and not observation[key]["timeout"]
                    and (updated[key] is None
                         or updated[key]["exit_code"] != 0
                         or updated[key]["timeout"])
                    for key in observation
                )
                # An edit that leaves the failure as it was did not help;
                # keeping it would make the final patch larger than needed.
                ineffective = (
                    kind == "edit" and not passed(updated)
                    and failure_text(updated) == failure_text(observation))
                if ineffective and not stakeholder_baseline:
                    record["ineffective"] = True
                    record["error_detail"] = (
                        "The edit did not change the failure, so it was "
                        "undone")
                # Undo the edit if a command that passed now fails.
                if (regressed or ineffective) and not stakeholder_baseline:
                    for name, text in before.items():
                        if name in created:
                            (root / name).unlink()
                        else:
                            (root / name).write_text(text)
                    record["rolled_back"] = True
                else:
                    observation = updated
            except ValueError as error:
                # The action could not be applied; tell the Fixer why.
                reject(record, str(error))
            except Exception as error:
                # Save a safe error name without model reply text.
                record["error"] = type(error).__name__
                if (isinstance(error, urllib.error.HTTPError)
                        and error.code == 403):
                    record["error_detail"] = (
                        "The model gateway refused the request to review "
                        "this action, so it was not applied. Send a "
                        "different, simpler action.")
                if isinstance(error, ModelResponseError):
                    report["reason"] = str(error)
                else:
                    report["reason"] = (
                        "Invalid action or infrastructure failure ("
                        + type(error).__name__ + ")")
                model_failures += 1
                if (isinstance(error, AgentTimeLimitError)
                        or model_failures >= 3):
                    break
        else:
            # The loop used every round; keep the last problem for the user.
            if (not passed(observation)
                    and report["reason"] != "Round limit reached"):
                report["reason"] = (
                    "Round limit reached. Last problem: " + report["reason"])

        # Set the final status and make the patch for the user.
        report["final"] = observation
        if environment_commands:
            report["environment_commands"] = environment_commands
        if passed(observation):
            if observation["tests"] is not None:
                report["status"] = "passed"
                report["reason"] = "Execution and detected tests passed"
            else:
                report["status"] = "execution_only"
                report["reason"] = (
                    "Execution passed; functionality not verified "
                    "(no tests detected)")
        final_files = {}
        for name in originals:
            try:
                final_files[name] = (root / name).read_text()
            except (OSError, UnicodeDecodeError):
                # A command removed the file or made it binary.
                continue
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
