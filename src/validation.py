"""Check actions sent by repair agents."""

from pathlib import PurePosixPath
import re

from repository import IGNORED


def validate_action(action, root):
    # Only allow the three action types used by the repair loop.
    if (not isinstance(action, dict)
            or not isinstance(action.get("action"), str)
            or action["action"] not in {"install", "edit", "give_up"}):
        raise ValueError("Invalid action; expected install, edit, or give_up")
    if not isinstance(action.get("reason"), str) or not action["reason"].strip():
        raise ValueError("Action requires a reason")
    # A package name must be plain text with no command flags.
    if action["action"] == "install":
        if (not isinstance(action.get("package"), str)
                or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.-]*", action["package"])):
            raise ValueError("Only plain package names are allowed")
    if action["action"] != "edit":
        return

    # A code edit must have at least one file and no more than 20.
    changes = action.get("files")
    if not isinstance(changes, dict) or not changes or len(changes) > 20:
        raise ValueError("Edit requires 1-20 files")
    for name, content in changes.items():
        if not isinstance(name, str) or not isinstance(content, str):
            raise ValueError("File names and contents must be strings")
        # Stop paths that try to leave the work copy.
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ValueError("Unsafe edit path")
        target = (root / name).resolve()
        if root.resolve() not in target.parents:
            raise ValueError("Edit escapes working copy")
        # Only allow edits to Python code that is not a test file.
        if (target.suffix != ".py" or not target.is_file()
                or any(part.lower().startswith("test") for part in path.parts)
                or any(part.startswith(".") for part in path.parts)
                or IGNORED.intersection(path.parts)):
            raise ValueError(
                "Only existing non-test Python source files may be edited")
        if len(content) > 100000:
            raise ValueError("Proposed file exceeds size limit")
