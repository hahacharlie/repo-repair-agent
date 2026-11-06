"""Read command options and start a repair run."""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys

from agents import ModelAgents, ScriptedAgents
from config import create_env_file, load_config, load_env
import constants as ct
from repair import repair


def python_version_error(version_info):
    # Stop early when this agent cannot safely run on the installed Python.
    current = tuple(version_info[:2])
    if current >= ct.MINIMUM_PYTHON:
        return None
    return (
        "Repository Repair Agent requires Python 3.10 or newer.\n"
        f"Current Python version: {current[0]}.{current[1]}.\n"
    )


def resolve_repository(value):
    # Use the path as given when it exists.
    if value.exists():
        return value.resolve()

    # A folder name means a repo saved in input/broken_repos.
    if value.parent == Path("."):
        saved_repo = ct.BROKEN_REPOS / value.name
        if saved_repo.is_dir():
            return saved_repo.resolve()
    raise ValueError("Repo not found: " + str(value))


def resolve_script(repository, script):
    # Use the script given by the user when there is one.
    if script is not None:
        return script

    # Saved test repos can keep their repair script in the repo folder.
    saved_script = repository / "repair.json"
    return saved_script if saved_script.is_file() else None


def write_results(results_root, repo_name, report):
    # Keep each repo result in its own folder.
    result_dir = results_root / repo_name
    fixed_dir = result_dir / "fixed_files"
    result_dir.mkdir(parents=True, exist_ok=True)

    # Remove files from the last run before saving the new ones.
    if fixed_dir.exists():
        shutil.rmtree(fixed_dir)

    fixed_files = report.get("fixed_files", {})
    for name, text in fixed_files.items():
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ValueError("Unsafe result file path")
        target = fixed_dir.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    # Keep full model prompts and replies in a separate local debug log.
    logs_path = result_dir / "agent_logs.json"
    agent_logs = report.get("agent_logs")
    if agent_logs:
        logs_path.write_text(json.dumps(agent_logs, indent=2) + "\n")
    elif logs_path.exists():
        logs_path.unlink()

    # The report lists file names, while the files hold the code.
    saved_report = dict(report)
    saved_report.pop("agent_logs", None)
    if agent_logs:
        saved_report["agent_log_file"] = "agent_logs.json"
    saved_report["fixed_files"] = sorted(fixed_files)
    (result_dir / "report.json").write_text(
        json.dumps(saved_report, indent=2) + "\n")
    # Remove the legacy patch artifact; changed files are saved above.
    patch_path = result_dir / "repair.patch"
    if patch_path.exists():
        patch_path.unlink()
    steps_path = result_dir / "steps_to_run.txt"
    steps = report.get("steps_to_run")
    if steps:
        steps_path.write_text(steps.rstrip() + "\n")
    elif steps_path.exists():
        steps_path.unlink()
    return result_dir


def main(version_info=None):
    # Check Python before reading files or starting a repair.
    error = python_version_error(
        sys.version_info if version_info is None else version_info)
    if error:
        print(error, end="", file=sys.stderr)
        return 2

    # Read the repo path and other options from the command line.
    parser = argparse.ArgumentParser(
        description="Repair a trusted repository in a temporary copy.")
    parser.add_argument("repository", type=Path)
    parser.add_argument(
        "--script", type=Path, help="Offline JSON action/review sequence")
    parser.add_argument(
        "--config",
        type=Path,
        default=ct.DEFAULT_CONFIG,
        help="Model configuration JSON (default: config.json in project root)",
    )
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument(
        "--agent-timeout",
        type=int,
        default=ct.DEFAULT_AGENT_TIMEOUT,
        help=(
            "Total seconds allowed inside Fixer/Judge API calls "
            "(default: 1200, maximum: 3600)"),
    )
    parser.add_argument(
        "--agent-logs",
        action="store_true",
        help="Save each Fixer and Judge prompt and response",
    )
    installs = parser.add_mutually_exclusive_group()
    installs.add_argument(
        "--allow-install",
        dest="allow_install",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    installs.add_argument(
        "--no-install",
        dest="allow_install",
        action="store_false",
        help="Do not install target repository dependencies",
    )
    parser.set_defaults(allow_install=True)
    parser.add_argument(
        "--backend",
        choices=("auto", "local", "docker"),
        default="auto",
        help=(
            "Where to run the temporary work copy (default: auto; use Docker "
            "when the repository supplies a Dockerfile)"),
    )
    parser.add_argument(
        "--docker-image",
        help="Override the Docker image selected from repository markers",
    )
    parser.add_argument(
        "--allow-docker-pull",
        action="store_true",
        help="Approve downloading a missing selected Docker image",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("results"),
        help="Folder used to save repair results")
    args = parser.parse_args()
    if not (ct.MINIMUM_AGENT_TIMEOUT
            <= args.agent_timeout <= ct.MAXIMUM_AGENT_TIMEOUT):
        parser.error(
            "--agent-timeout must be from "
            + str(ct.MINIMUM_AGENT_TIMEOUT) + " to "
            + str(ct.MAXIMUM_AGENT_TIMEOUT) + " seconds")

    try:
        repository = resolve_repository(args.repository)
    except ValueError as error:
        parser.error(str(error))
    script = resolve_script(repository, args.script)

    # Live model runs need a settings file and API key.
    config = None
    if not script:
        # Create the local key file on the first model-backed run.
        created_env = create_env_file(ct.DEFAULT_ENV, ct.API_KEY_ENV)
        load_env(ct.DEFAULT_ENV)
        if created_env and not os.environ.get(ct.API_KEY_ENV):
            parser.exit(
                2,
                "Created .env file at:\n" + str(ct.DEFAULT_ENV) + "\n"
                "Add your API key in this file.\n"
                "Then run the command again.\n",
            )
        try:
            config = load_config(args.config)
        except (ValueError, OSError):
            parser.error(
                "Cannot load model config; check --config path and fields")
        if not os.environ.get(ct.API_KEY_ENV):
            parser.exit(
                2,
                "Add your API key in this file:\n"
                + str(ct.DEFAULT_ENV) + "\n"
                "Then run the command again.\n",
            )

    try:
        # Script mode is used for tests and does not call a model.
        if script:
            agents = ScriptedAgents(json.loads(script.read_text()))
        else:
            agents = ModelAgents(
                config,
                capture_logs=args.agent_logs,
                time_budget_seconds=args.agent_timeout,
            )
        print("Repairing repository: " + repository.name, flush=True)
        report = repair(
            repository,
            agents,
            args.rounds,
            args.timeout,
            args.allow_install,
            args.backend,
            args.docker_image,
            args.allow_docker_pull,
            progress=lambda message: print(message, flush=True),
        )
    except Exception as error:
        raise
        parser.exit(2, "Repair setup failed: " + type(error).__name__ + "\n")

    # Save the report, patch, and changed files.
    result_dir = write_results(args.output, repository.name, report)

    # Print a short result that is easy to read.
    print()
    if report.get("no_repair_needed"):
        print("No errors found. No repair was needed.")
    elif report["status"] == "passed":
        print("Repair completed successfully.")
    elif report["status"] == "execution_only":
        print("The app runs, but no tests were found.")
    else:
        print("Repair could not be completed.")
        print("Reason: " + report["reason"])

    final = report.get("final", {})
    app = final.get("app")
    tests = final.get("tests")
    if app:
        app_result = "passed" if app["exit_code"] == 0 and not app["timeout"] else "failed"
        label = "Application: " if report["repository"]["language"] == "python" else "Build: "
        print(label + app_result)
    if tests:
        test_result = "passed" if tests["exit_code"] == 0 and not tests["timeout"] else "failed"
        print("Tests: " + test_result)
    else:
        print("Tests: none found")

    fixed_files = sorted(report.get("fixed_files", {}))
    if fixed_files:
        print("Fixed files:")
        for name in fixed_files:
            print("  - " + name)
    else:
        print("Fixed files: none")
    saved_to = result_dir / "fixed_files" if fixed_files else result_dir
    print("Results saved to: " + str(saved_to))
    if (result_dir / "steps_to_run.txt").is_file():
        print("Run instructions: " + str(result_dir / "steps_to_run.txt"))
    if (result_dir / "agent_logs.json").is_file():
        print("Agent logs: " + str(result_dir / "agent_logs.json"))
    return 0 if report["status"] in {"passed", "execution_only"} else 1
