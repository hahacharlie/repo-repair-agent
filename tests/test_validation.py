from pathlib import Path
import unittest

from validation import validate_action


BROKEN_REPO = (
    Path(__file__).resolve().parents[1]
    / "input"
    / "broken_repos"
    / "undefined_greeting_variable"
)


class ValidationTests(unittest.TestCase):
    def test_invalid_paths_and_test_edits_are_rejected(self):
        for name in (
                "../outside.py", "/tmp/outside.py", "tests/test_greeting.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_action(
                    {"action": "edit", "reason": "bad", "files": {name: ""}},
                    BROKEN_REPO,
                )

    def test_source_suffixes_follow_the_repository_language(self):
        root = (
            Path(__file__).resolve().parents[1]
            / "input" / "broken_repos" / "fzf"
        )
        edit = {"action": "edit", "reason": "fix", "files": {"main.go": ""}}
        validate_action(edit, root, {".go"})
        with self.assertRaises(ValueError):
            validate_action(edit, root)
        with self.assertRaises(ValueError):
            validate_action({
                "action": "edit", "reason": "fix",
                "files": {"src/options_test.go": ""},
            }, root, {".go"})

    def test_nested_action_is_rejected_cleanly(self):
        for action in ({"type": "edit"}, ["edit"], None, 42):
            with self.subTest(action=action), self.assertRaises(ValueError):
                validate_action(
                    {"action": action, "reason": "Invalid schema"},
                    BROKEN_REPO,
                )

    def test_package_arguments_cannot_inject_options(self):
        for package in (
                "--upgrade", "thing;whoami", "https://example.com/x", "x y"):
            with self.subTest(package=package), self.assertRaises(ValueError):
                validate_action({
                    "action": "install",
                    "reason": "bad",
                    "package": package,
                }, BROKEN_REPO)


if __name__ == "__main__":
    unittest.main()
