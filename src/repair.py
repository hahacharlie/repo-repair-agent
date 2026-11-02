"""Main code repair loop."""

import difflib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from agents import ModelAgents, ModelResponseError
from execution import run_command
from repository import IGNORED, inspect_repository, snapshot
from validation import validate_action


def diff_files(before, after):
    # Show the lines changed between the old and new files.
    return "".join(
        "".join(difflib.unified_diff(
            before[name].splitlines(keepends=True),
            after[name].splitlines(keepends=True),
            fromfile="a/" + name,
            tofile="b/" + name,
        ))
        for name in sorted(after)
        if before[name] != after[name]
    )


def repair(source, agents, rounds=5, timeout=30, allow_install=False):
    # Stop bad limits before any work starts.
    if rounds < 1 or timeout <= 0:
        raise ValueError("Rounds and timeout must be positive")
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

    # All repair work happens in a short-lived folder.
    with tempfile.TemporaryDirectory(prefix="repo-repair-") as temporary:
        root = Path(temporary) / "repo"

        # Links may point outside the work copy, so do not allow them.
        if any(path.is_symlink() for path in source.rglob("*")
               if not IGNORED.intersection(path.relative_to(source).parts)):
            raise ValueError("Symlinks are unsupported")
        # Copy the repo so the source files stay unchanged.
        shutil.copytree(source, root, ignore=shutil.ignore_patterns(*IGNORED))
        metadata = inspect_repository(root)
        report["repository"] = metadata

        # Make a clean Python setup for this repair run.
        env = Path(temporary) / "env"
        setup = run_command(
            [
                "uv", "--no-cache", "venv", "--no-project",
                "--python", sys.executable, "--no-python-downloads", str(env),
            ],
            root,
            timeout,
        )
        report["environment_setup"] = setup
        if setup["exit_code"] != 0 or setup["timeout"]:
            report["reason"] = "uv environment creation failed"
            return report

        python = str(
            env / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))

        def verify():
            # Run the app first, then run tests when they exist.
            app = run_command([python, metadata["entry"]], root, timeout)
            tests = None
            if metadata["test_command"]:
                tests = run_command(
                    [python] + metadata["test_command"], root, timeout)
            return {"app": app, "tests": tests}

        def passed(observation):
            # Every command must finish on time with exit code zero.
            return all(
                not item["timeout"] and item["exit_code"] == 0
                for item in observation.values()
                if item is not None
            )

        # Install listed packages only when the user allows it.
        if allow_install and (root / "requirements.txt").is_file():
            setup = run_command(
                [
                    "uv", "pip", "install", "--python", python,
                    "-r", "requirements.txt",
                ],
                root,
                timeout,
            )
            report["setup"] = setup
            if setup["exit_code"] != 0 or setup["timeout"]:
                report["reason"] = "Declared dependency installation failed"
                return report

        # Save the first app and test result before asking for a fix.
        observation = verify()
        report["initial"] = observation
        originals = {}
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
                    "files": snapshot(root),
                    "observation": observation,
                    "history": report["history"][:-1],
                })
                validate_action(action, root)
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
                        record["error"] = (
                            "Dependency installation disabled; use --allow-install")
                        continue
                    record["installation"] = run_command(
                        [
                            "uv", "pip", "install", "--python", python,
                            action["package"],
                        ],
                        root,
                        timeout,
                    )
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
    return report
