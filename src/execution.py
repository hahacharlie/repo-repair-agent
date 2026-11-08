"""Run commands and collect their output."""

import os
from pathlib import Path
import re
import signal
import subprocess


def _replace_path(text, path, label):
    # Handle both Windows and Unix path styles.
    forms = {str(path), str(path).replace("\\", "/")}
    for form in sorted(forms, key=len, reverse=True):
        if form:
            text = re.sub(re.escape(form), label, text, flags=re.IGNORECASE)
    return text


def _hide_local_paths(text, command, cwd):
    # Hide full command paths first so the report stays easy to read.
    for item in command:
        path = Path(str(item))
        if not path.is_absolute():
            continue
        name = path.name.lower()
        if name.startswith("python"):
            label = "<python>"
        elif name == "env":
            label = "<environment>"
        else:
            label = "<path>"
        text = _replace_path(text, path, label)

    # Hide any other paths printed in errors or setup messages.
    repo = Path(cwd).resolve()
    text = _replace_path(text, repo, "<repo>")
    text = _replace_path(text, repo.parent, "<run>")
    text = _replace_path(text, Path.home().resolve(), "<home>")
    return text


def _safe_command(command, cwd):
    return [
        _hide_local_paths(str(item), command, cwd)
        for item in command
    ]


OUTPUT_LIMIT = 16000
FAILURE_LINE = re.compile(
    r"(^|\s)(FAIL|FAILED|ERROR|error\b|error\[|panic|panicked|Traceback|"
    r"Exception|fatal|undefined|cannot find|No module named)", re.IGNORECASE)


def _trim_output(text):
    """Keep the tail of long output, led by its failure lines.

    Build and test logs can run to megabytes; the lines that name a failure
    are often far from the end, so they are kept before the tail.
    """
    if len(text) <= OUTPUT_LIMIT:
        return text
    tail = text[-(OUTPUT_LIMIT * 3 // 4):]
    head = text[:-(OUTPUT_LIMIT * 3 // 4)]
    failures = [line for line in head.splitlines() if FAILURE_LINE.search(line)]
    summary = "\n".join(failures)[:OUTPUT_LIMIT // 4]
    if not summary:
        return tail
    return summary + "\n...[output trimmed]...\n" + tail


def run_command(command, cwd, timeout, on_timeout=None):
    # Use UTF-8 so output works the same on every system.
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"

    # Start the command inside the work copy.
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=(os.name == "posix"),
        )
    except OSError:
        # A missing runner (for example Docker) is an execution result, not a
        # Python traceback that would hide the actionable setup problem.
        return {
            "command": _safe_command(command, cwd),
            "exit_code": 127,
            "stdout": "",
            "stderr": "Command is unavailable: " + str(command[0]),
            "timeout": False,
        }
    try:
        # Wait for the command, but do not wait past the time limit.
        out, err = process.communicate(timeout=timeout)
        return {
            "command": _safe_command(command, cwd),
            "exit_code": process.returncode,
            "stdout": _hide_local_paths(_trim_output(out), command, cwd),
            "stderr": _hide_local_paths(_trim_output(err), command, cwd),
            "timeout": False,
        }
    except subprocess.TimeoutExpired:
        # Stop the command when it takes too long.
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        if on_timeout is not None:
            on_timeout()
        out, err = process.communicate()
        return {
            "command": _safe_command(command, cwd),
            "exit_code": process.returncode,
            "stdout": _hide_local_paths(_trim_output(out), command, cwd),
            "stderr": _hide_local_paths(_trim_output(err), command, cwd),
            "timeout": True,
        }
