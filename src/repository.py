"""Read basic facts and useful files from a code repo."""

from pathlib import Path


IGNORED = {".git", ".venv", "venv", "__pycache__", "node_modules", "results"}


def detect_repository(root):
    """Identify a build system and a suitable default container image."""
    root = Path(root)
    markers = (
        ("go.mod", "go", "go", "golang:1.22-bookworm"),
        ("Cargo.toml", "rust", "cargo", "rust:1.78-bookworm"),
        ("package.json", "javascript", "npm", "node:22-bookworm-slim"),
        ("pom.xml", "java", "maven", "maven:3.9-eclipse-temurin-21"),
        ("build.gradle", "java", "gradle", "gradle:8-jdk21"),
        ("build.gradle.kts", "kotlin", "gradle", "gradle:8-jdk21"),
        ("CMakeLists.txt", "cpp", "cmake", "ubuntu:24.04"),
    )
    for marker, language, build_system, image in markers:
        if (root / marker).is_file():
            return language, build_system, image

    python_markers = ("pyproject.toml", "requirements.txt", "setup.py")
    if any((root / marker).is_file() for marker in python_markers):
        return "python", "python", "python:3.12-slim"
    if any(root.glob("*.py")):
        return "python", "python", "python:3.12-slim"
    if (root / "Makefile").is_file():
        return "unknown", "make", "ubuntu:24.04"
    return "unknown", "unknown", "ubuntu:24.04"


def inspect_repository(root):
    root = Path(root)
    language, build_system, docker_image = detect_repository(root)
    # Look for a Python file that can start the app.
    entry = next(
        (name for name in ("main.py", "app.py", "run.py")
         if (root / name).is_file()),
        None,
    )
    if not entry:
        if language != "python":
            return {
                "language": language,
                "build_system": build_system,
                "docker_image": docker_image,
                "requires_linux": (root / "Dockerfile").is_file(),
                "entry": None,
                "tests": [],
                "test_command": None,
            }
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
        "language": language,
        "build_system": build_system,
        "docker_image": docker_image,
        "requires_linux": (root / "Dockerfile").is_file(),
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
