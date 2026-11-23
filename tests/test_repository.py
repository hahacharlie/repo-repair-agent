from pathlib import Path
import tempfile
import unittest

import constants as ct
from repository import (
    command_planning_context,
    detect_repository,
    inspect_repository,
    snapshot,
)


def make_repo(files):
    folder = tempfile.TemporaryDirectory()
    root = Path(folder.name)
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return folder, root


class DetectionTests(unittest.TestCase):
    def test_command_planning_context_is_root_only_and_bounded(self):
        folder, root = make_repo({
            "README.md": "Build with the project tool.\n",
            "CONTRIBUTING.md": "Run project-tool check.\n",
            "Cargo.toml": "[package]\nname = 'demo'\n",
            "src/main.rs": "fn main() {}\n",
            "large.lock": "x" * 70000,
        })
        with folder:
            (root / "binary.dat").write_bytes(b"text\0binary")
            context = command_planning_context(root, root / "README.md")

        self.assertEqual(
            context["readme"], "Build with the project tool.\n")
        self.assertEqual(sorted(context["root_files"]), [
            "CONTRIBUTING.md", "Cargo.toml"])
        self.assertNotIn("README.md", context["root_files"])
        self.assertNotIn("binary.dat", context["root_files"])
        self.assertNotIn("large.lock", context["root_files"])
        self.assertNotIn("src/main.rs", context["root_files"])
        self.assertLessEqual(
            len(context["readme"])
            + sum(map(len, context["root_files"].values())),
            ct.COMMAND_CONTEXT_CHARS,
        )

    def test_build_system_markers_follow_benchmark_layouts(self):
        cases = (
            # git ships Cargo.toml and Makefile beside its meson build.
            ({"Cargo.toml": "", "meson.build": "", "Makefile": ""}, "c", "meson"),
            # godot keeps a pyproject.toml for tooling but builds with SCons.
            ({"SConstruct": "", "pyproject.toml": ""}, "cpp", "scons"),
            # FFmpeg uses a configure script and make.
            ({"configure": "", "Makefile": ""}, "c", "make"),
            # uv is a Rust project with Python packaging metadata.
            ({"Cargo.toml": "", "pyproject.toml": ""}, "rust", "cargo"),
            ({"go.mod": "", "Makefile": ""}, "go", "go"),
            ({"CMakeLists.txt": "", "Makefile": ""}, "cpp", "cmake"),
            ({"main.py": ""}, "python", "python"),
        )
        for files, language, build_system in cases:
            folder, root = make_repo(files)
            with folder, self.subTest(files=sorted(files)):
                self.assertEqual(
                    detect_repository(root)[:2], (language, build_system))

    def test_cargo_repository_gets_build_and_test_commands(self):
        folder, root = make_repo({
            "Cargo.toml": "[package]\nname = \"demo\"\n",
            "src/main.rs": "fn main() {}\n",
            "tests/cli.rs": "#[test] fn works() {}\n",
        })
        with folder:
            metadata = inspect_repository(root)
            self.assertEqual(metadata["entry"], "Cargo.toml")
            self.assertEqual(metadata["build_command"], ["cargo", "build"])
            self.assertEqual(metadata["test_command"], ["cargo", "test"])
            self.assertEqual(metadata["tests"], ["tests/cli.rs"])
            self.assertEqual(sorted(snapshot(root, "cargo")), [
                "Cargo.toml", "src/main.rs", "tests/cli.rs"])

    def test_go_repository_without_tests_has_no_test_command(self):
        folder, root = make_repo({"go.mod": "module demo\n", "main.go": ""})
        with folder:
            metadata = inspect_repository(root)
            self.assertEqual(
                metadata["build_command"], ["go", "build", "./..."])
            self.assertIsNone(metadata["test_command"])
            self.assertEqual(metadata["tests"], [])

    def test_python_version_selects_matching_docker_image(self):
        folder, root = make_repo({
            "main.py": "print('ok')\n",
            ".python-version": "3.11\n",
        })
        with folder:
            metadata = inspect_repository(root)
            self.assertEqual(metadata["python_requirement"], "3.11")
            self.assertEqual(metadata["docker_image"], "python:3.11-slim")

        folder, root = make_repo({
            "main.py": "print('ok')\n",
            "pyproject.toml": (
                "[project]\nname = 'demo'\n"
                "requires-python = '>=3.10,<3.12'\n"),
        })
        with folder:
            metadata = inspect_repository(root)
            self.assertEqual(
                metadata["python_requirement"], ">=3.10,<3.12")
            self.assertEqual(metadata["docker_image"], "python:3.10-slim")


if __name__ == "__main__":
    unittest.main()
