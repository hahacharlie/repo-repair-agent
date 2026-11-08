"""Read basic facts and useful files from a code repo."""

from pathlib import Path
import re

import constants as ct


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
        "image": ct.DEFAULT_PYTHON_IMAGE,
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
        return "python", "python", ct.DEFAULT_PYTHON_IMAGE
    if any(root.glob("*.py")):
        return "python", "python", ct.DEFAULT_PYTHON_IMAGE
    if (root / "Makefile").is_file():
        return "c", "make", TOOLCHAINS["make"]["image"]
    return "unknown", "unknown", "ubuntu:24.04"


def has_tests(root, rule):
    if rule is None:
        return []
    if rule[0] == "glob":
        return sorted(
            path.relative_to(root).as_posix() for path in root.glob(rule[1])
            if not IGNORED.intersection(path.relative_to(root).parts))
    path = root / rule[1]
    if path.is_file() and re.search(rule[2], path.read_text(errors="replace"),
                                    re.MULTILINE):
        return [rule[1]]
    return []


def required_python(root):
    """Read the Python version requested by common project files."""
    version_file = root / ".python-version"
    if version_file.is_file():
        for line in version_file.read_text(errors="replace").splitlines():
            value = line.strip()
            if value and not value.startswith("#"):
                return value

    project_file = root / "pyproject.toml"
    if not project_file.is_file():
        return None
    text = project_file.read_text(errors="replace")
    project = re.search(
        r"(?ms)^\[project\]\s*$\n(.*?)(?=^\[|\Z)", text)
    if not project:
        return None
    match = re.search(
        r"(?m)^\s*requires-python\s*=\s*['\"]([^'\"]+)['\"]",
        project.group(1),
    )
    return match.group(1).strip() if match else None


def python_docker_image(requirement):
    """Choose a Python image when a clear version is requested."""
    if not requirement:
        return TOOLCHAINS["python"]["image"]
    match = re.fullmatch(
        r"(?:cpython-)?(3\.\d+(?:\.\d+)?)", requirement.strip())
    if not match:
        match = re.search(
            r"(?:==|~=|>=)\s*(3\.\d+(?:\.\d+)?)", requirement)
    if not match:
        return TOOLCHAINS["python"]["image"]
    return "python:" + match.group(1) + "-slim"


def inspect_repository(root):
    root = Path(root)
    language, build_system, docker_image = detect_repository(root)
    python_requirement = required_python(root) if language == "python" else None
    if python_requirement:
        docker_image = python_docker_image(python_requirement)
    metadata = {
        "language": language,
        "build_system": build_system,
        "docker_image": docker_image,
        "python_requirement": python_requirement,
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
        path.relative_to(root).as_posix()
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


def command_planning_context(root, readme):
    """Read a bounded README and root-file snapshot for command planning."""
    root = Path(root)
    readme = Path(readme)
    remaining = ct.COMMAND_CONTEXT_CHARS

    def read_text(path, limit):
        # Read only enough bytes to fill the prompt budget, even for huge files.
        with path.open("rb") as stream:
            data = stream.read((limit * 4) + 1)
        if b"\0" in data:
            return None
        value = data.decode("utf-8", errors="replace")
        # Give the model the same line endings on every operating system.
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        truncated = len(value) > limit or path.stat().st_size > len(data)
        if not truncated:
            return value
        if limit <= len(ct.COMMAND_CONTEXT_TRUNCATED):
            return value[:limit]
        content_limit = max(0, limit - len(ct.COMMAND_CONTEXT_TRUNCATED))
        return value[:content_limit] + ct.COMMAND_CONTEXT_TRUNCATED

    readme_text = read_text(
        readme, min(ct.COMMAND_CONTEXT_FILE_CHARS, remaining))
    if readme_text is None:
        raise ValueError("Repository README is not a text file")
    remaining -= len(readme_text)

    root_files = {}
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if remaining <= 0:
            break
        if (path == readme or path.name in IGNORED or path.is_symlink()
                or not path.is_file()):
            continue
        file_limit = min(ct.COMMAND_CONTEXT_FILE_CHARS, remaining)
        if path.stat().st_size > file_limit:
            continue
        value = read_text(
            path, file_limit)
        if value is None:
            continue
        root_files[path.name] = value
        remaining -= len(value)

    return {"readme": readme_text, "root_files": root_files}


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
        if path.stat().st_size > ct.AGENT_SNAPSHOT_FILE_CHARS:
            continue
        value = path.read_text(errors="replace")
        if size + len(value) > ct.AGENT_SNAPSHOT_CHARS:
            continue
        content[relative.as_posix()] = value
        size += len(value)
    return content
