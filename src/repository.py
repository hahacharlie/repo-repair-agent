"""Read basic facts and useful files from a code repo."""

from pathlib import Path
import re


IGNORED = {".git", ".venv", "venv", "__pycache__", "node_modules", "results",
           ".home"}

# One row per build system: image, source suffixes, dependency fetch command,
# command that adds one dependency, build command, test command, and the glob
# or file-content pattern that shows tests exist. None means "not available".
TOOLCHAINS = {
    "cargo": {
        "image": "rust:bookworm",
        "sources": {".rs", ".toml"},
        "fetch": ["cargo", "fetch"],
        "add": ["cargo", "add"],
        "manifests": ["Cargo.toml", "Cargo.lock"],
        "build": ["cargo", "build"],
        "test": ["cargo", "test"],
        "tests": ("glob", "tests/**/*.rs"),
    },
    "go": {
        "image": "golang:bookworm",
        "sources": {".go", ".mod"},
        "fetch": ["go", "mod", "download"],
        "add": ["go", "get"],
        "manifests": ["go.mod", "go.sum"],
        "build": ["go", "build", "./..."],
        "test": ["go", "test", "./..."],
        "tests": ("glob", "**/*_test.go"),
    },
    "cmake": {
        "image": "ubuntu:24.04",
        "sources": {".c", ".h", ".cc", ".cpp", ".hpp", ".txt", ".cmake"},
        "build": ["sh", "-c", "cmake -S . -B build && cmake --build build"],
        "test": ["ctest", "--test-dir", "build", "--output-on-failure"],
        "tests": ("content", "CMakeLists.txt", r"\badd_test\s*\("),
    },
    "meson": {
        "image": "ubuntu:24.04",
        "sources": {".c", ".h", ".cc", ".cpp", ".hpp", ".build"},
        "build": ["sh", "-c", "meson setup build && meson compile -C build"],
        "test": ["meson", "test", "-C", "build"],
        "tests": ("content", "meson.build", r"^\s*test\s*\("),
    },
    "scons": {
        "image": "ubuntu:24.04",
        "sources": {".c", ".h", ".cc", ".cpp", ".hpp", "SConstruct", "SCsub"},
        "build": ["scons"],
        "test": None,
        "tests": None,
    },
    "make": {
        "image": "ubuntu:24.04",
        "sources": {".c", ".h", ".cc", ".cpp", ".hpp", ".mak", ".mk"},
        "build": ["sh", "-c", "{ [ ! -x ./configure ] || ./configure; } && make"],
        "test": ["make", "check"],
        "tests": ("content", "Makefile", r"^check\s*:"),
    },
    "python": {
        "image": "python:3.12-slim",
        "sources": {".py", ".md", ".txt", ".toml", ".cfg", ".ini"},
        "manifests": ["requirements.txt"],
    },
}


def detect_repository(root):
    """Identify a build system and a suitable default container image."""
    root = Path(root)
    # Order matters: git and scrcpy keep Cargo.toml or build.gradle beside
    # their meson build, and godot has a pyproject.toml beside SConstruct.
    markers = (
        ("meson.build", "c", "meson"),
        ("SConstruct", "cpp", "scons"),
        ("go.mod", "go", "go"),
        ("Cargo.toml", "rust", "cargo"),
        ("CMakeLists.txt", "cpp", "cmake"),
        ("configure", "c", "make"),
    )
    for marker, language, build_system in markers:
        if (root / marker).is_file():
            return language, build_system, TOOLCHAINS[build_system]["image"]

    python_markers = ("pyproject.toml", "requirements.txt", "setup.py")
    if any((root / marker).is_file() for marker in python_markers):
        return "python", "python", "python:3.12-slim"
    if any(root.glob("*.py")):
        return "python", "python", "python:3.12-slim"
    if (root / "Makefile").is_file():
        return "c", "make", TOOLCHAINS["make"]["image"]
    return "unknown", "unknown", "ubuntu:24.04"


def has_tests(root, rule):
    if rule is None:
        return []
    if rule[0] == "glob":
        return sorted(
            str(path.relative_to(root)) for path in root.glob(rule[1])
            if not IGNORED.intersection(path.relative_to(root).parts))
    path = root / rule[1]
    if path.is_file() and re.search(rule[2], path.read_text(errors="replace"),
                                    re.MULTILINE):
        return [rule[1]]
    return []


def inspect_repository(root):
    root = Path(root)
    language, build_system, docker_image = detect_repository(root)
    metadata = {
        "language": language,
        "build_system": build_system,
        "docker_image": docker_image,
        "requires_linux": (root / "Dockerfile").is_file(),
        "entry": None,
        "tests": [],
        "test_command": None,
    }
    if language != "python":
        if build_system == "unknown":
            return metadata
        toolchain = TOOLCHAINS[build_system]
        # The marker file doubles as the mount check target.
        entry = next(
            name for name in (
                "meson.build", "SConstruct", "go.mod", "Cargo.toml",
                "CMakeLists.txt", "configure", "Makefile")
            if (root / name).is_file())
        tests = has_tests(root, toolchain["tests"])
        metadata.update({
            "entry": entry,
            "build_command": toolchain["build"],
            "tests": tests,
            "test_command": toolchain["test"] if tests else None,
        })
        return metadata

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
    metadata.update({"entry": entry, "tests": tests, "test_command": test_command})
    return metadata


def source_suffixes(build_system):
    return TOOLCHAINS.get(build_system, TOOLCHAINS["python"])["sources"]


def snapshot(root, build_system="python"):
    """Read up to 60 KB of useful repo text for an agent."""
    content = {}
    size = 0
    suffixes = source_suffixes(build_system)

    # Read small text files until the size limit is reached.
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or IGNORED.intersection(relative.parts):
            continue
        if path.suffix not in suffixes and path.name not in suffixes:
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
