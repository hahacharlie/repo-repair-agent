"""Read basic facts and useful files from a code repo."""


IGNORED = {".git", ".venv", "venv", "__pycache__", "node_modules", "results"}


def inspect_repository(root):
    # Look for a Python file that can start the app.
    entry = next(
        (name for name in ("main.py", "app.py", "run.py")
         if (root / name).is_file()),
        None,
    )
    if not entry:
        raise ValueError(
            "Supported repositories require main.py, app.py, or run.py at root")
    # Find Python test files outside folders we do not use.
    tests = sorted(
        str(path.relative_to(root))
        for path in root.rglob("test*.py")
        if not IGNORED.intersection(path.relative_to(root).parts)
    )
    # Build the test command only when tests are found.
    test_command = None
    if tests:
        test_command = [
            "-m", "unittest", "discover", "-s",
            "tests" if (root / "tests").is_dir() else ".",
        ]
    return {
        "language": "python",
        "entry": entry,
        "tests": tests,
        "test_command": test_command,
    }


def snapshot(root):
    """Read up to 60 KB of useful repo text for an agent."""
    content = {}
    size = 0

    # Read small text files until the size limit is reached.
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or IGNORED.intersection(relative.parts):
            continue
        if path.suffix not in {".py", ".md", ".txt", ".toml", ".cfg", ".ini"}:
            continue
        # Skip large files so the model input stays small.
        if path.stat().st_size > 20000:
            continue
        value = path.read_text(errors="replace")
        if size + len(value) > 60000:
            continue
        content[relative.as_posix()] = value
        size += len(value)
    return content
