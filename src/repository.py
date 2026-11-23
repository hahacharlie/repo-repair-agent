"""Read basic facts and useful files from a code repo."""

import os
from pathlib import Path, PurePosixPath
import re

import constants as ct


# Never add plain project folder names here: a project can ship a source
# package that is named, for example, "results".
IGNORED = {".git", ".venv", "venv", "__pycache__", "node_modules", ".home"}

# Build and configuration files that a repair may need in any language.
BUILD_FILES = {
    ".toml", ".json", ".yaml", ".yml", ".cfg", ".ini", ".txt", ".cmake",
    ".mk", ".mak", ".in", ".sh", ".py", ".build", ".options", ".gradle",
    ".kts", ".proto", ".m4", ".ac", ".am", ".mod", ".sum", ".lock",
    "Makefile", "GNUmakefile", "SConstruct", "SCsub", "configure",
}

C_SOURCES = {
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".inc", ".S", ".asm",
    ".m", ".mm", ".rs", ".java", ".lua", ".vim",
}

# One row per build system: image, source suffixes, dependency fetch command,
# command that adds one dependency, build command, test command, and the glob
# or file-content pattern that shows tests exist. None means "not available".
TOOLCHAINS = {
    "cargo": {
        "image": ct.SANDBOX_IMAGE,
        "sources": {".rs"} | BUILD_FILES,
        "fetch": ["cargo", "fetch"],
        "add": ["cargo", "add"],
        "manifests": ["Cargo.toml", "Cargo.lock"],
        "build": ["cargo", "build"],
        "test": ["cargo", "test"],
        "tests": ("glob", "tests/**/*.rs"),
    },
    "go": {
        "image": ct.SANDBOX_IMAGE,
        "sources": {".go", ".s"} | BUILD_FILES,
        "fetch": ["go", "mod", "download"],
        "add": ["go", "get"],
        "manifests": ["go.mod", "go.sum"],
        "build": ["go", "build", "./..."],
        "test": ["go", "test", "./..."],
        "tests": ("glob", "**/*_test.go"),
    },
    "cmake": {
        "image": ct.SANDBOX_IMAGE,
        "sources": C_SOURCES | BUILD_FILES,
        "build": ["sh", "-c", "cmake -S . -B build && cmake --build build"],
        "test": ["ctest", "--test-dir", "build", "--output-on-failure"],
        "tests": ("content", "CMakeLists.txt", r"\badd_test\s*\("),
    },
    "meson": {
        "image": ct.SANDBOX_IMAGE,
        "sources": C_SOURCES | BUILD_FILES,
        "build": ["sh", "-c", "meson setup build && meson compile -C build"],
        "test": ["meson", "test", "-C", "build"],
        "tests": ("content", "meson.build", r"^\s*test\s*\("),
    },
    "scons": {
        "image": ct.SANDBOX_IMAGE,
        "sources": C_SOURCES | BUILD_FILES,
        "build": ["scons"],
        "test": None,
        "tests": None,
    },
    "make": {
        "image": ct.SANDBOX_IMAGE,
        "sources": C_SOURCES | BUILD_FILES,
        "build": ["sh", "-c", "{ [ ! -x ./configure ] || ./configure; } && make"],
        "test": ["make", "check"],
        "tests": ("content", "Makefile", r"^check\s*:"),
    },
    "python": {
        "image": ct.DEFAULT_PYTHON_IMAGE,
        "sources": {".py", ".md"} | BUILD_FILES,
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
    return "unknown", "unknown", ct.SANDBOX_IMAGE


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


# Words that show a build or test step in CI files and contributor guides.
CI_TOOLS = {
    "go": r"\bgo (test|build|run|vet|generate)\b|\bmake\b|gotestsum",
    "cargo": r"\bcargo\b",
    "cmake": r"\bcmake\b|\bctest\b|\bmake\b|\bninja\b",
    "meson": r"\bmeson\b|\bninja\b",
    "scons": r"\bscons\b",
    "make": r"\bmake\b|\./configure",
}
CI_FILES = (
    ".github/workflows/*.yml", ".github/workflows/*.yaml", ".gitlab-ci.yml",
    ".circleci/config.yml", "CONTRIBUTING*", "BUILD*.md", "INSTALL*",
    "HACKING*", "DEVELOP*.md", "docs/CONTRIBUTING*", "docs/BUILD*.md",
    "docs/develop*.md",
)
CI_CONTEXT_CHARS = 6000


def ci_commands(root, build_system):
    """Return lines of CI and contributor files that name build tools."""
    tools = CI_TOOLS.get(build_system)
    if tools is None:
        return {}
    root = Path(root)
    found = {}
    size = 0
    for pattern in CI_FILES:
        for path in sorted(root.glob(pattern)):
            if (size >= CI_CONTEXT_CHARS or not path.is_file()
                    or path.is_symlink() or path.stat().st_size > 400000):
                continue
            lines = []
            for line in path.read_text(errors="replace").splitlines():
                line = line.strip().lstrip("-").strip()
                if (re.search(tools, line) and not line.startswith("#")
                        and line not in lines):
                    lines.append(line[:300])
            if lines:
                found[path.relative_to(root).as_posix()] = lines[:12]
                size += sum(map(len, lines[:12]))
    return found


def command_planning_context(root, readme=None, build_system=None):
    """Read a bounded README and root-file snapshot for command planning."""
    root = Path(root)
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

    readme_text = None
    if readme is not None:
        readme = Path(readme)
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

    context = {"readme": readme_text, "root_files": root_files}
    toolchain = TOOLCHAINS.get(build_system)
    if toolchain is not None and "build" in toolchain:
        context["default_commands"] = {
            "dependency_command": toolchain.get("fetch"),
            "build_command": toolchain["build"],
            "test_command": toolchain["test"],
        }
        found = ci_commands(root, build_system)
        if found:
            context["ci_commands"] = found
    return context


def is_test_path(name):
    """Return whether a relative path names a test or test data.

    Test names look like test_x.py, tests/, foo_test.go, testdata/, or the
    top-level t/ folder.
    """
    parts = PurePosixPath(name).parts
    return (
        any(re.search(r"(^|_)tests?($|[._])", part.lower())
            or part.lower() in {
                "testdata", "snapshots", "__snapshots__", "fixtures"}
            for part in parts)
        or (len(parts) > 1 and parts[0] == "t"))


def file_manifest(root):
    """Record the size and time of every work-copy file."""
    root = Path(root)
    manifest = {}
    for folder, folders, files in os.walk(root):
        folders[:] = [name for name in folders if name not in IGNORED]
        for name in files:
            path = Path(folder) / name
            try:
                status = path.lstat()
            except OSError:
                continue
            manifest[path.relative_to(root).as_posix()] = (
                status.st_size, status.st_mtime_ns)
    return manifest


def changed_files(root, manifest, names=None):
    """Return manifest files that are now missing or different."""
    root = Path(root)
    changed = []
    for name in (manifest if names is None else names):
        try:
            status = (root / name).lstat()
        except OSError:
            changed.append(name)
            continue
        if (status.st_size, status.st_mtime_ns) != manifest[name]:
            changed.append(name)
    return changed


def root_listing(root):
    """List the top-level entries so an agent knows the repository layout."""
    entries = sorted(
        path.name + ("/" if path.is_dir() else "")
        for path in Path(root).iterdir() if path.name not in IGNORED)
    return entries[:ct.ROOT_LISTING_ENTRIES]


def mentioned_files(root, text, build_system="python"):
    """Return repository source files whose paths appear in command output."""
    root = Path(root)
    suffixes = source_suffixes(build_system)
    prefix = "/workspace/" + root.name + "/"
    found = []
    for token in re.findall(r"[A-Za-z0-9_/][A-Za-z0-9_./+-]*", text):
        if token.startswith(prefix):
            token = token[len(prefix):]
        token = token.strip("./")
        if not token or ".." in token.split("/") or len(token) > 300:
            continue
        path = root / token
        if (path not in found and path.is_file()
                and (path.suffix in suffixes or path.name in suffixes)
                and not IGNORED.intersection(Path(token).parts)):
            found.append(path)
    return found


def search_repository(root, pattern, folder=None):
    """Return up to MAX_SEARCH_MATCHES "path:line: text" hits for a substring."""
    root = Path(root)
    matches = []
    start = root / folder if folder else root
    # The search area is one file or every file below a folder.
    for path in [start] if start.is_file() else sorted(start.rglob("*")):
        relative = path.relative_to(root)
        try:
            if (IGNORED.intersection(relative.parts) or path.is_symlink()
                    or not path.is_file()
                    or path.stat().st_size > ct.MAX_SEARCH_FILE_BYTES):
                continue
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if pattern not in text:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern in line:
                matches.append(
                    relative.as_posix() + ":" + str(number) + ": "
                    + line.strip()[:200])
                if len(matches) >= ct.MAX_SEARCH_MATCHES:
                    return matches
    return matches


def read_repository_file(root, name, start=None, end=None):
    """Read numbered lines of one work-copy file, bounded for a prompt."""
    lines = (Path(root) / name).read_text(errors="replace").splitlines()
    first = max(1, start or 1)
    last = min(len(lines), end or len(lines))
    output = []
    size = 0
    for number in range(first, last + 1):
        line = str(number) + ": " + lines[number - 1]
        size += len(line) + 1
        if size > ct.MAX_READ_CHARS:
            output.append(
                "...[stopped at line " + str(number - 1) + " of "
                + str(len(lines)) + "; read a line range for more]")
            break
        output.append(line)
    return "\n".join(output)


def snapshot(root, build_system="python", priority=(), known=None):
    """Read up to 60 KB of useful repo text for an agent.

    When known is given, only those relative paths are sent. This keeps
    build output, such as logs, out of the prompt.

    Files in priority (for example the files that the error output names)
    come first. A small repository is then sent whole. A large one adds only
    its root files, because the agent can read and search the rest.
    """
    root = Path(root)
    content = {}
    size = 0
    suffixes = source_suffixes(build_system)
    priority = list(priority)
    # Walk top-down and skip ignored folders, so the dependency caches of a
    # large repository are never listed.
    candidates = []
    limit = ct.SNAPSHOT_SMALL_REPOSITORY_FILES
    for folder, folders, files in os.walk(root):
        folders[:] = sorted(name for name in folders if name not in IGNORED)
        candidates.extend(
            Path(folder) / name for name in sorted(files)
            if Path(name).suffix in suffixes or name in suffixes)
        if len(candidates) > limit:
            candidates = [path for path in candidates if path.parent == root]
            break
    candidates = [path for path in candidates if path not in priority]

    # Read small text files until the size limit is reached.
    for path in priority + candidates:
        relative = path.relative_to(root)
        if not path.is_file() or IGNORED.intersection(relative.parts):
            continue
        if path.suffix not in suffixes and path.name not in suffixes:
            continue
        if known is not None and relative.as_posix() not in known:
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
