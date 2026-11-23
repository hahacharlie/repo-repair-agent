"""Check actions sent by repair agents."""

from pathlib import PurePosixPath
import re

import constants as ct
from repository import IGNORED, is_test_path


# These actions read the work copy and never change it.
EXPLORATIONS = {"read", "search", "inspect"}
# These actions need the Docker sandbox.
SHELL_ACTIONS = {"inspect", "run", "commands"}
ACTIONS = {"install", "edit", "give_up", "run", "commands"} | EXPLORATIONS
# A build must compile the local source. These commands fetch a published
# copy of a project instead.
REMOTE_BUILD = re.compile(
    r"\bgo\s+install\s+\S+@\S+|\bgit\s+clone\b"
    r"|\bcargo\s+install\b(?!.*--path)")
# The planned commands do not run as root, so they cannot install system
# packages. The agents install those as reviewed repair steps.
SYSTEM_INSTALL = re.compile(
    r"\b(sudo|apt|apt-get|dnf|yum|pacman|apk|brew|zypper)\b")
# A test command must not hide the result of the tests.
HIDDEN_FAILURE = re.compile(r"\|\|\s*(true|:|exit\s+0)\b|;\s*(true|exit\s+0)\s*$")


def check_path(name, root):
    """Reject paths that leave the work copy; return the real path."""
    if not isinstance(name, str) or not name:
        raise ValueError("A path must be a relative file name")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError(
            "Unsafe path '" + name + "'; use a path relative to the "
            "repository root")
    target = (root / name).resolve()
    if root.resolve() not in target.parents:
        raise ValueError("Path escapes working copy: " + name)
    if IGNORED.intersection(path.parts):
        raise ValueError("Path is in an ignored folder: " + name)
    return target


def check_shell(value, field):
    if (not isinstance(value, str) or not value.strip()
            or len(value) > ct.MAX_SHELL_COMMAND_CHARS):
        raise ValueError(
            field + " must be one shell command line of 1-"
            + str(ct.MAX_SHELL_COMMAND_CHARS) + " characters")


def check_edit_target(name, root, suffixes):
    """Allow a non-test source file that exists or that the project lacks."""
    target = check_path(name, root)
    path = PurePosixPath(name)
    if ((target.suffix not in suffixes and target.name not in suffixes)
            or (target.exists() and not target.is_file())
            or target.is_symlink()
            or is_test_path(name)
            or any(part.startswith(".") for part in path.parts)):
        raise ValueError(
            "Only non-test source files may be edited, not '" + name + "'")
    return target


def validate_action(action, root, suffixes=frozenset({".py"}), shell=False):
    """Raise ValueError with a message that tells the Fixer what to correct."""
    if (not isinstance(action, dict)
            or not isinstance(action.get("action"), str)
            or action["action"] not in ACTIONS):
        raise ValueError(
            "Invalid action; expected one of " + ", ".join(sorted(ACTIONS)))
    kind = action["action"]
    # A look at the repository needs no reason; the Judge never reviews it.
    if kind not in EXPLORATIONS and (
            not isinstance(action.get("reason"), str)
            or not action["reason"].strip()):
        raise ValueError("Action requires a reason")
    if kind in SHELL_ACTIONS and not shell:
        raise ValueError(
            "The " + kind + " action needs the Docker sandbox, which this "
            "run does not use")

    if kind == "read":
        target = check_path(action.get("path"), root)
        if not target.is_file():
            raise ValueError("File to read does not exist: " + action["path"])
        for field in ("start", "end"):
            value = action.get(field)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(field + " must be a line number from 1")
    elif kind == "search":
        pattern = action.get("pattern")
        if (not isinstance(pattern, str) or not pattern.strip()
                or len(pattern) > ct.MAX_SEARCH_PATTERN_CHARS):
            raise ValueError(
                "search requires a pattern of 1-"
                + str(ct.MAX_SEARCH_PATTERN_CHARS) + " characters")
        if action.get("path") not in (None, "", "."):
            if not check_path(action["path"], root).exists():
                raise ValueError(
                    "Path to search does not exist: " + action["path"])
    elif kind in {"inspect", "run"}:
        check_shell(action.get("command"), "command")
        for field in ("root", "verify"):
            if type(action.get(field, False)) is not bool:
                raise ValueError(field + " must be true or false")
    elif kind == "commands":
        check_shell(action.get("build_command"), "build_command")
        if REMOTE_BUILD.search(action["build_command"]):
            raise ValueError(
                "build_command must compile the local source, not download "
                "or install a published copy")
        test_command = action.get("test_command")
        if test_command is not None:
            check_shell(test_command, "test_command")
            if HIDDEN_FAILURE.search(test_command):
                raise ValueError("test_command must not hide a test failure")
        evidence = action.get("evidence", [])
        if (not isinstance(evidence, list)
                or len(evidence) > ct.MAX_EVIDENCE_FILES):
            raise ValueError(
                "evidence must list at most " + str(ct.MAX_EVIDENCE_FILES)
                + " repository files")
        for name in evidence:
            if not check_path(name, root).is_file():
                raise ValueError("Evidence file does not exist: " + str(name))
    elif kind == "install":
        # A package name must be plain text with no command flags.
        if (not isinstance(action.get("package"), str)
                or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.-]*", action["package"])):
            raise ValueError("Only plain package names are allowed")
    if kind != "edit":
        return

    # A code edit must have a small, bounded set of files.
    files = action.get("files", {})
    replacements = action.get("replacements", {})
    if (not isinstance(files, dict) or not isinstance(replacements, dict)
            or not (files or replacements)
            or len(files) + len(replacements) > ct.MAX_EDIT_FILES):
        raise ValueError(
            "Edit requires files or replacements for 1-"
            + str(ct.MAX_EDIT_FILES) + " files")
    for name, content in files.items():
        if not isinstance(name, str) or not isinstance(content, str):
            raise ValueError("File names and contents must be strings")
        check_edit_target(name, root, suffixes)
        if len(content) > ct.MAX_EDIT_FILE_CHARS:
            raise ValueError("Proposed file exceeds size limit")
    for name, changes in replacements.items():
        if not isinstance(name, str) or name in files:
            raise ValueError("Give each file once, in files or replacements")
        if not check_edit_target(name, root, suffixes).is_file():
            raise ValueError(
                "replacements needs an existing file, not '" + name + "'")
        if (not isinstance(changes, list) or not changes
                or not all(
                    isinstance(change, dict)
                    and isinstance(change.get("old"), str) and change["old"]
                    and isinstance(change.get("new"), str)
                    for change in changes)):
            raise ValueError(
                "replacements must map each file to a list of "
                '{"old": "...", "new": "..."} objects')


def apply_replacements(text, changes, name):
    """Return text after each old string is replaced exactly once."""
    for change in changes:
        count = text.count(change["old"])
        if count != 1:
            raise ValueError(
                "old text occurs " + str(count) + " times in '" + name
                + "', but it must occur exactly once: "
                + change["old"][:120])
        text = text.replace(change["old"], change["new"])
    return text
