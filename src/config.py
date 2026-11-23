"""Read and check the settings file."""

import json
import os
import re
from urllib.parse import urlsplit

import constants as ct


def create_env_file(path, variable):
    # Create a safe first-run file without adding a real key.
    if path.exists():
        return False
    path.write_text(
        "# Add your API key below, then run the command again.\n"
        + variable + "=\n"
    )
    return True


def load_env(path):
    # Do nothing when the local file is not there.
    if not path.is_file():
        return

    # Read simple NAME=value lines from the local file.
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, mark, value = line.partition("=")
        name = name.strip()
        if not mark or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue

        # Keep a value that was already set outside this file. An empty
        # exported variable must not hide the value in the file.
        if os.environ.get(name):
            continue
        value = value.strip()
        if (len(value) >= 2 and value[0] == value[-1]
                and value[0] in {"'", '"'}):
            value = value[1:-1]
        os.environ[name] = value


def load_config(path):
    # Read the JSON settings from the given file.
    config = json.loads(path.read_text())
    fields = {"endpoint", "fixer_model", "judge_model"}
    optional_fields = {"api_timeout_seconds"}

    # Check that all needed fields are present.
    if (not isinstance(config, dict) or not fields <= set(config)
            or set(config) - fields - optional_fields):
        raise ValueError("Config requires: " + ", ".join(sorted(fields)))

    # Each main setting must be text and must not be empty.
    if any(not isinstance(config[field], str) or not config[field].strip()
           for field in fields):
        raise ValueError("Config values must be non-empty strings")

    # Keep the wait time between 1 second and 1 hour.
    timeout = config.get("api_timeout_seconds", ct.DEFAULT_API_TIMEOUT)
    if (type(timeout) is not int
            or not ct.MINIMUM_API_TIMEOUT
            <= timeout <= ct.MAXIMUM_API_TIMEOUT):
        raise ValueError(
            "api_timeout_seconds must be an integer from "
            + str(ct.MINIMUM_API_TIMEOUT) + " to "
            + str(ct.MAXIMUM_API_TIMEOUT)
        )

    # Only allow a safe HTTPS model URL with no key in the URL.
    endpoint = urlsplit(config["endpoint"])
    if (endpoint.scheme != "https" or not endpoint.hostname
            or endpoint.username or endpoint.password):
        raise ValueError(
            "Config endpoint must be an HTTPS URL without embedded credentials")
    return config
