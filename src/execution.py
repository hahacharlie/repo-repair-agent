"""Run commands and collect their output."""

import os
import signal
import subprocess


def run_command(command, cwd, timeout):
    # Start the command inside the work copy.
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=(os.name == "posix"),
    )
    try:
        # Wait for the command, but do not wait past the time limit.
        out, err = process.communicate(timeout=timeout)
        return {
            "command": command,
            "exit_code": process.returncode,
            "stdout": out[-16000:],
            "stderr": err[-16000:],
            "timeout": False,
        }
    except subprocess.TimeoutExpired:
        # Stop the command when it takes too long.
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        out, err = process.communicate()
        return {
            "command": command,
            "exit_code": process.returncode,
            "stdout": out[-16000:],
            "stderr": err[-16000:],
            "timeout": True,
        }
