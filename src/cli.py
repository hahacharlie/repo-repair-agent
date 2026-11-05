"""Read command options and start a repair run."""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shutil

from agents import API_KEY_ENV, ModelAgents, ScriptedAgents
from config import load_config, load_env
from repair import repair


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config.json"
DEFAULT_ENV = PROJECT_ROOT / ".env"
BROKEN_REPOS = PROJECT_ROOT / "tests" / "broken_repos"


def resolve_repository(value):
    # Use the path as given when it exists.
    if value.exists():
        return value.resolve()

    # A folder name means a repo saved in tests/broken_repos.
    if value.parent == Path("."):
        saved_repo = BROKEN_REPOS / value.name
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

    # The report lists file names, while the files hold the code.
    saved_report = dict(report)
    saved_report["fixed_files"] = sorted(fixed_files)
    (result_dir / "report.json").write_text(
        json.dumps(saved_report, indent=2) + "\n")
    (result_dir / "repair.patch").write_text(report["diff"])
    steps_path = result_dir / "steps_to_run.txt"
    steps = report.get("steps_to_run")
    if steps:
        steps_path.write_text(steps.rstrip() + "\n")
    elif steps_path.exists():
        steps_path.unlink()
    return result_dir


def main():
    # Load the local API key file when it is present.
    load_env(DEFAULT_ENV)

    # Read the repo path and other options from the command line.
    parser = argparse.ArgumentParser(
        description="Repair a trusted repository in a temporary copy.")
    parser.add_argument("repository", type=Path)
    parser.add_argument(
        "--script", type=Path, help="Offline JSON action/review sequence")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Model configuration JSON (default: config.json in project root)",
    )
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=30)
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

    try:
        repository = resolve_repository(args.repository)
    except ValueError as error:
        parser.error(str(error))
    script = resolve_script(repository, args.script)

    # Live model runs need a settings file and API key.
    config = None
    if not script:
        try:
            config = load_config(args.config)
        except (ValueError, OSError):
            parser.error(
                "Cannot load model config; check --config path and fields")
        if not os.environ.get(API_KEY_ENV):
            parser.error(
                "Set " + API_KEY_ENV + " or use --script for offline operation")

    try:
        # Script mode is used for tests and does not call a model.
        if script:
            agents = ScriptedAgents(json.loads(script.read_text()))
        else:
            agents = ModelAgents(config)
        print("Repairing repository: " + repository.name)
        report = repair(
            repository,
            agents,
            args.rounds,
            args.timeout,
            args.allow_install,
            args.backend,
            args.docker_image,
            args.allow_docker_pull,
        )
    except Exception as error:
        raise
        parser.exit(2, "Repair setup failed: " + type(error).__name__ + "\n")

    # Save the report, patch, and changed files.
    result_dir = write_results(args.output, repository.name, report)

    # Print a short result that is easy to read.
    print()
    if report["status"] == "passed":
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
    print("Fixed files: " + (", ".join(fixed_files) if fixed_files else "none"))
    saved_to = result_dir / "fixed_files" if fixed_files else result_dir
    print("Results saved to: " + str(saved_to))
    if (result_dir / "steps_to_run.txt").is_file():
        print("Run instructions: " + str(result_dir / "steps_to_run.txt"))
    return 0 if report["status"] in {"passed", "execution_only"} else 1
