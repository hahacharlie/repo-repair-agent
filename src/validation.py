"""Check actions sent by repair agents."""

from pathlib import PurePosixPath
import re

import constants as ct
from repository import IGNORED


def split_line_range(entry):
    """Split "path:START-END" into the path and a (start, end) pair or None."""
    match = re.fullmatch(r"(.+):(\d+)-(\d+)", entry)
    if not match:
        return entry, None
    start, end = int(match.group(2)), int(match.group(3))
    if start < 1 or end < start:
        raise ValueError("Invalid line range: " + entry)
    return match.group(1), (start, end)


def check_path(name, root):
    """Reject paths that leave the work copy; return the posix and real path."""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or not name:
        raise ValueError("Unsafe path: " + name)
    target = (root / name).resolve()
    if root.resolve() not in target.parents:
        raise ValueError("Path escapes working copy: " + name)
    if IGNORED.intersection(path.parts):
        raise ValueError("Path is in an ignored folder: " + name)
    return path, target


def validate_action(action, root, suffixes=frozenset({".py"})):
    # Only allow the action types used by the repair loop.
    if (not isinstance(action, dict)
            or not isinstance(action.get("action"), str)
            or action["action"] not in {
                "install", "edit", "give_up", "search", "read"}):
        raise ValueError(
            "Invalid action; expected install, edit, search, read, or give_up")
    if not isinstance(action.get("reason"), str) or not action["reason"].strip():
        raise ValueError("Action requires a reason")
    if action["action"] == "search":
        pattern = action.get("pattern")
        if (not isinstance(pattern, str) or not pattern.strip()
                or len(pattern) > ct.MAX_SEARCH_PATTERN_CHARS):
            raise ValueError(
                "search requires a pattern of 1-"
                + str(ct.MAX_SEARCH_PATTERN_CHARS) + " characters")
        return
    if action["action"] == "read":
        names = action.get("files")
        if (not isinstance(names, list) or not names
                or len(names) > ct.MAX_READ_FILES
                or not all(isinstance(name, str) for name in names)):
            raise ValueError(
                "read requires a list of 1-" + str(ct.MAX_READ_FILES)
                + " relative file paths")
        for entry in names:
            name, _ = split_line_range(entry)
            check_path(name, root)
            if not (root / name).is_file():
                raise ValueError("File to read does not exist: " + name)
        return
    # A package name must be plain text with no command flags.
    if action["action"] == "install":
        if (not isinstance(action.get("package"), str)
                or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.-]*", action["package"])):
            raise ValueError("Only plain package names are allowed")
    if action["action"] != "edit":
        return

    # A code edit must have a small, bounded set of files.
    changes = action.get("files")
    if (not isinstance(changes, dict) or not changes
            or len(changes) > ct.MAX_EDIT_FILES):
        raise ValueError(
            "Edit requires 1-" + str(ct.MAX_EDIT_FILES) + " files")
    for name, content in changes.items():
        if not isinstance(name, str) or not isinstance(content, str):
            raise ValueError("File names and contents must be strings")
        path, target = check_path(name, root)
        # Only allow edits to source code that is not a test file. Test names
        # look like test_x.py, tests/, foo_test.go, or integration_tests.rs.
        if ((target.suffix not in suffixes and target.name not in suffixes)
                or not target.is_file()
                or any(re.search(r"(^|_)tests?($|[._])", part.lower())
                       for part in path.parts)
                or any(part.startswith(".") for part in path.parts)
                or IGNORED.intersection(path.parts)):
            raise ValueError(
                "Only existing non-test source files may be edited")
        if len(content) > ct.MAX_EDIT_FILE_CHARS:
            raise ValueError("Proposed file exceeds size limit")
