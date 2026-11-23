"""Tests for the sandbox actions that let the two agents repair a setup."""

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from agents import (
    ModelAgents, ModelResponseError, ScriptedAgents, parse_model_json,
    reduce_context, shell_command)
from backends import DockerBackend
from config import load_config
import constants as ct
from repair import has_mixed_cosmetic_changes, ran_no_tests, repair
from repository import (
    ci_commands, is_test_path, mentioned_files, read_repository_file,
    search_repository, snapshot)
from validation import apply_replacements, validate_action


ROOT = Path(__file__).resolve().parents[1]
BUILD = ["sh", "-c", "{ [ ! -x ./configure ] || ./configure; } && make"]
INSTALL = "apt-get update && apt-get install -y libfoo-dev"


def result(exit_code=0, stderr=""):
    return {
        "command": ["docker"], "exit_code": exit_code, "stdout": "",
        "stderr": stderr, "timeout": False,
    }


def make_repo(folder, files):
    root = Path(folder) / "demo"
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return root


class Recorder(ScriptedAgents):
    """Keep each context that the Fixer and the Judge receive."""

    def __init__(self, steps):
        super().__init__(steps)
        self.contexts = []
        self.reviews = []

    def propose(self, context):
        self.contexts.append(context)
        return super().propose(context)

    def review(self, context):
        self.reviews.append(context)
        return super().review(context)


class Sandbox:
    """Stand in for the container: the build needs a library and a file."""

    def __init__(self):
        self.calls = []
        self.library = False
        self.test_failures = 0

    def run(self, command, cwd, timeout, network=False, user=None):
        del timeout, network
        self.calls.append((command, user))
        text = command[-1]
        if text == INSTALL:
            self.library = True
        elif "tests/check.c" in text:
            (Path(cwd) / "tests/check.c").write_text("int main(){return 0;}\n")
        elif text == "cat Makefile":
            return dict(result(), stdout="all:\n")
        elif command == ["make", "check"] and self.test_failures:
            self.test_failures -= 1
            return result(1, "FAIL: timing")
        elif command == BUILD and not self.library:
            return result(1, "fatal error: foo.h: No such file or directory")
        elif text == "make BROKEN=1":
            return result(2, "make: *** No rule to make target")
        return result()


def run_repair(root, agents, sandbox, rounds=6):
    backend = DockerBackend("sandbox:test")
    with patch.object(
            backend, "availability_status", return_value=result()), patch.object(
            backend, "image_status", return_value=result()), patch.object(
            backend, "run", side_effect=sandbox.run), patch(
            "repair.make_backend", return_value=backend):
        return repair(root, agents, rounds=rounds, backend="auto")


def step(action, approved=True):
    return {"action": dict(action, reason="test"), "approved": approved}


FILES = {
    "Makefile": "all:\n\tcc main.c\ncheck:\n\t./a.out\n",
    "main.c": "#include <foo.h>\nint main(){return 0;}\n",
    "tests/check.c": "int main(){return 1;}\n",
}


class SandboxActionTests(unittest.TestCase):
    def test_approved_command_repairs_the_environment_as_root(self):
        sandbox = Sandbox()
        with tempfile.TemporaryDirectory() as folder:
            report = run_repair(
                make_repo(folder, FILES),
                ScriptedAgents([step(
                    {"action": "run", "command": INSTALL, "root": True})]),
                sandbox)

        self.assertEqual(report["status"], "passed")
        self.assertIn((["sh", "-c", INSTALL], "root"), sandbox.calls)
        self.assertEqual(
            report["environment_commands"],
            [{"command": INSTALL, "root": True}])
        self.assertIn(INSTALL, report["steps_to_run"])
        self.assertTrue(
            report["metrics"]["correct_repair_rate"]["correct_repairs"])

    def test_failed_tests_run_once_more_before_a_repair_starts(self):
        sandbox = Sandbox()
        sandbox.library = True
        sandbox.test_failures = 1
        with tempfile.TemporaryDirectory() as folder:
            report = run_repair(
                make_repo(folder, FILES), ScriptedAgents([]), sandbox)

        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["no_repair_needed"])
        self.assertEqual(report["final"]["tests"]["attempts"], 2)
        self.assertIn("second run", report["tests_note"])

    def test_planned_system_install_is_not_used_as_dependency_command(self):
        sandbox = Sandbox()
        install = ["sh", "-c", "sudo apt install -y libfoo-dev"]
        plan = {
            "dependency_command": install, "build_command": BUILD,
            "test_command": ["make", "check"]}
        give_up = {"action": "give_up", "reason": "stop"}
        with patch.dict(os.environ, {ct.API_KEY_ENV: "test-token"}):
            agents = ModelAgents(load_config(ROOT / "config.json"))
        with tempfile.TemporaryDirectory() as folder, patch.object(
                agents, "plan_commands", return_value=plan), patch.object(
                agents, "propose", return_value=give_up), patch.object(
                agents, "generate_run_steps", return_value="steps"):
            report = run_repair(make_repo(folder, FILES), agents, sandbox)

        self.assertIsNone(report["command_plan"]["dependency_command"])
        self.assertIn("root", report["dependency_command_note"])
        self.assertNotIn((install, None), sandbox.calls)
        self.assertIn((BUILD, None), sandbox.calls)

    def test_rejected_command_never_runs(self):
        sandbox = Sandbox()
        with tempfile.TemporaryDirectory() as folder:
            report = run_repair(
                make_repo(folder, FILES),
                ScriptedAgents([step(
                    {"action": "run", "command": INSTALL, "root": True},
                    approved=False)]),
                sandbox)

        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["history"][0]["judge"]["approved"])
        self.assertNotIn((["sh", "-c", INSTALL], "root"), sandbox.calls)

    def test_command_cannot_change_the_tests(self):
        sandbox = Sandbox()
        change = "echo 'int main(){return 0;}' > tests/check.c"
        with tempfile.TemporaryDirectory() as folder:
            root = make_repo(folder, FILES)
            report = run_repair(
                root, ScriptedAgents([step(
                    {"action": "run", "command": change})]), sandbox)
            self.assertEqual(
                (root / "tests/check.c").read_text(), FILES["tests/check.c"])

        self.assertEqual(
            report["history"][0]["restored_test_files"], ["tests/check.c"])

    def test_exploration_uses_no_round_and_reaches_the_fixer(self):
        sandbox = Sandbox()
        agents = Recorder([
            step({"action": "read", "path": "main.c", "start": 1, "end": 1}),
            step({"action": "search", "pattern": "foo.h"}),
            step({"action": "inspect", "command": "cat Makefile"}),
            step({"action": "run", "command": INSTALL, "root": True}),
        ])
        with tempfile.TemporaryDirectory() as folder:
            report = run_repair(make_repo(folder, FILES), agents, sandbox)

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["explorations"], 3)
        self.assertEqual(len(report["history"]), 1)
        explored = agents.contexts[-1]["explored"]
        self.assertEqual(explored[0]["result"], "1: #include <foo.h>")
        self.assertIn("main.c:1: #include <foo.h>", explored[1]["result"])
        self.assertIn("all:", explored[2]["result"])
        # An inspect command runs as a user that cannot write the work copy.
        self.assertIn((["sh", "-c", "cat Makefile"], "reader"), sandbox.calls)
        self.assertEqual(agents.contexts[-1]["budget"]["rounds_left"], 6)

    def test_same_action_is_permitted_again_after_the_failure_changed(self):
        sandbox = Sandbox()
        install = step({"action": "run", "command": INSTALL, "root": True})
        broken = step({"action": "commands", "build_command": "make BROKEN=1",
                       "test_command": "make check"})
        with tempfile.TemporaryDirectory() as folder:
            report = run_repair(
                make_repo(folder, FILES),
                # The Judge rejects the install twice; the failure changes
                # between the second and the third try.
                ScriptedAgents([
                    dict(install, approved=False),
                    dict(install, approved=False), broken, install]),
                sandbox)

        errors = [record.get("error") for record in report["history"]]
        self.assertEqual(errors[1], "Repeated action rejected")
        self.assertIsNone(errors[3])
        self.assertIn("command_result", report["history"][3])

    def test_exploration_after_the_limit_uses_a_round(self):
        sandbox = Sandbox()
        look = step({"action": "search", "pattern": "foo.h"})
        with tempfile.TemporaryDirectory() as folder:
            report = run_repair(
                make_repo(folder, FILES),
                ScriptedAgents([look] * (ct.EXPLORATIONS_PER_ROUND + 1)),
                sandbox, rounds=1)

        self.assertEqual(
            report["explorations"], ct.EXPLORATIONS_PER_ROUND + 1)
        self.assertEqual(len(report["history"]), 1)
        self.assertTrue(report["history"][0]["exploration"])

    def test_new_commands_need_the_judge_and_keep_the_tests(self):
        sandbox = Sandbox()
        agents = Recorder([
            step({"action": "commands", "build_command": "make all",
                  "test_command": None}),
            step({"action": "commands", "build_command": "make all",
                  "test_command": "make check", "evidence": ["Makefile"]}),
        ])
        with tempfile.TemporaryDirectory() as folder:
            report = run_repair(make_repo(folder, FILES), agents, sandbox)

        self.assertEqual(
            report["history"][0]["error_detail"],
            "The test command cannot be removed")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(
            report["final"]["tests"]["command"], ["docker"])
        self.assertIn((["sh", "-c", "make check"], None), sandbox.calls)
        self.assertEqual(agents.reviews[0]["kind"], "commands")
        # The Judge gets the sandbox details only.
        self.assertEqual(list(agents.reviews[0]["capabilities"]), ["shell"])
        self.assertIn("check:", agents.reviews[0]["evidence"]["Makefile"])

    def test_invalid_action_is_reported_and_the_repair_goes_on(self):
        sandbox = Sandbox()
        agents = Recorder([
            step({"action": "edit", "files": {"tests/check.c": ""}}),
            step({"action": "run", "command": INSTALL, "root": True}),
        ])
        with tempfile.TemporaryDirectory() as folder:
            report = run_repair(make_repo(folder, FILES), agents, sandbox)

        self.assertEqual(report["status"], "passed")
        self.assertIn("non-test", report["history"][0]["error_detail"])
        self.assertIn(
            "non-test", agents.contexts[1]["history"][0]["error_detail"])

    def test_edit_can_replace_text_and_add_a_missing_file(self):
        fix = step({
            "action": "edit",
            "replacements": {
                "main.py": [{"old": "helper.gret", "new": "helper.greet"}]},
            "files": {"helper.py": "def greet():\n    return 'hi'\n"},
        })
        with tempfile.TemporaryDirectory() as folder:
            root = make_repo(folder, {
                "main.py": "import helper\nprint(helper.gret())\n"})
            report = repair(root, ScriptedAgents([fix]))
            self.assertFalse((root / "helper.py").exists())

        self.assertEqual(report["status"], "execution_only")
        self.assertEqual(
            sorted(report["fixed_files"]), ["helper.py", "main.py"])
        self.assertIn("+def greet():", report["diff"])


    def test_edit_that_leaves_the_failure_unchanged_is_undone(self):
        edit = step({"action": "edit", "replacements": {
            "main.py": [{"old": "x = 1", "new": "x = 2"}]}})
        with tempfile.TemporaryDirectory() as folder:
            root = make_repo(folder, {
                "main.py": "x = 1\nraise SystemExit('still broken')\n"})
            report = repair(root, ScriptedAgents([edit]), rounds=1)

        self.assertTrue(report["history"][0]["ineffective"])
        self.assertTrue(report["history"][0]["rolled_back"])
        self.assertEqual(report["diff"], "")


class ActionRuleTests(unittest.TestCase):
    def test_shell_actions_need_the_sandbox(self):
        with tempfile.TemporaryDirectory() as folder:
            root = make_repo(folder, FILES)
            run = {"action": "run", "command": "make", "reason": "r"}
            validate_action(run, root, {".c"}, shell=True)
            # Only a repair action needs a reason.
            validate_action({"action": "read", "path": "main.c"}, root)
            with self.assertRaisesRegex(ValueError, "reason"):
                validate_action(
                    {"action": "run", "command": "make"}, root, shell=True)
            with self.assertRaisesRegex(ValueError, "Docker sandbox"):
                validate_action(run, root, {".c"})
            for action in (
                    {"action": "run", "command": ""},
                    {"action": "run", "command": "make", "root": "yes"},
                    {"action": "read", "path": "missing.c"},
                    {"action": "read", "path": "../main.c"},
                    {"action": "search", "pattern": ""},
                    {"action": "commands", "build_command": "make",
                     "test_command": "make check || true"},
                    {"action": "commands", "build_command": "make",
                     "test_command": "make check", "evidence": ["none.md"]},
                    {"action": "commands", "test_command": "go test ./...",
                     "build_command": "go install example.com/tool@latest"},
                    {"action": "edit", "replacements": {
                        "main.c": [{"old": "", "new": "x"}]}},
                    {"action": "edit", "replacements": {
                        "new.c": [{"old": "a", "new": "b"}]}},
                    {"action": "edit", "files": {"testdata/x.c": ""}}):
                with self.subTest(action=action), self.assertRaises(
                        ValueError):
                    validate_action(
                        dict(action, reason="r"), root, {".c"}, shell=True)

    def test_replacement_text_must_occur_exactly_once(self):
        self.assertEqual(
            apply_replacements("a b", [{"old": "b", "new": "c"}], "f"), "a c")
        for text in ("a a", "b"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                apply_replacements(text, [{"old": "a", "new": "c"}], "f")

    def test_test_paths_cover_test_data(self):
        for name in ("tests/a.c", "pkg/a_test.go", "pkg/testdata/a.txt",
                     "t/t0001.sh", "src/snapshots/a.snap"):
            self.assertTrue(is_test_path(name), name)
        for name in ("src/main.c", "pkg/latest/a.go", "contest.py", "t.c"):
            self.assertFalse(is_test_path(name), name)

    def test_model_command_becomes_one_shell_line(self):
        self.assertEqual(
            shell_command("make -j4 && make check"),
            ["sh", "-c", "make -j4 && make check"])
        self.assertEqual(
            shell_command(["cmake", "--build", "build"]),
            ["cmake", "--build", "build"])
        self.assertEqual(
            shell_command(["meson", "setup", "b", "&&", "ninja", "-C", "b"]),
            ["sh", "-c", "meson setup b && ninja -C b"])
        with self.assertRaises(ValueError):
            shell_command({"command": "make"})


    def test_tool_call_style_reply_is_read_as_an_action(self):
        self.assertEqual(
            parse_model_json(
                ' <|tool_calls_section_begin|> <|tool_call_begin|> '
                'functions.search:0 <|tool_call_argument_begin|> '
                '{"pattern": "fn main", "reason": "r"} <|tool_call_end|> '
                '<|tool_calls_section_end|>'),
            {"pattern": "fn main", "reason": "r", "action": "search"})
        self.assertEqual(
            parse_model_json(
                'functions.Read:0 <|tool_call_argument_begin|> {"path": "a"}'),
            {"path": "a", "action": "read"})
        self.assertEqual(
            parse_model_json('{"action": "read"} <|tool_call_end|>'),
            {"action": "read"})
        with self.assertRaises(ModelResponseError):
            parse_model_json('I will search. {"action": "search"}')

    def test_test_command_that_runs_no_tests_is_detected(self):
        for output in (
                "Test project /workspace/x/build\nNo tests were found!!!\n",
                "\nRan 0 tests in 0.000s\n\nOK\n",
                "running 0 tests\n\nrunning 0 tests\n"):
            self.assertTrue(ran_no_tests({"stdout": output}), output)
        for output in (
                "running 0 tests\n\nrunning 12 tests\n",
                "Ran 10 tests in 0.1s\n", "ok  \tpkg\t0.1s\n"):
            self.assertFalse(ran_no_tests({"stdout": output}), output)

    def test_blank_lines_in_added_code_are_not_cosmetic(self):
        before = {"a.py": "def one():\n    return 1\n"}
        added = {"a.py": (
            "def one():\n    return 1\n\n\ndef two():\n    return 2\n")}
        self.assertFalse(has_mixed_cosmetic_changes(before, added))


class PromptContextTests(unittest.TestCase):
    def test_snapshot_puts_named_files_first_and_skips_build_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = make_repo(folder, dict(
                FILES, **{"build/log.txt": "cc main.c\n"}))
            named = mentioned_files(
                root, "/workspace/demo/main.c:1: fatal error", "make")
            files = snapshot(
                root, "make", named, {"Makefile", "main.c", "tests/check.c"})
            self.assertEqual(named, [root / "main.c"])
            self.assertEqual(list(files)[0], "main.c")
            self.assertNotIn("build/log.txt", files)
            self.assertEqual(
                search_repository(root, "foo.h"),
                ["main.c:1: #include <foo.h>"])
            # The search area can be one file.
            self.assertEqual(
                search_repository(root, "main", "main.c"),
                ["main.c:2: int main(){return 0;}"])
            validate_action(
                {"action": "search", "pattern": "main", "path": "main.c"},
                root)
            self.assertEqual(
                read_repository_file(root, "main.c", 2, 2),
                "2: int main(){return 0;}")

    def test_ci_and_contributor_files_show_test_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            root = make_repo(folder, {
                ".github/workflows/ci.yml": (
                    "steps:\n  - run: go test -short ./...\n  - run: ls\n"),
                "CONTRIBUTING.md": "Run `make test` before a pull request.\n",
            })
            self.assertEqual(ci_commands(root, "go"), {
                ".github/workflows/ci.yml": ["run: go test -short ./..."],
                "CONTRIBUTING.md": ["Run `make test` before a pull request."],
            })

    def test_refused_request_is_sent_again_with_less_context(self):
        context = {
            "files": {"a.c": "x"},
            "explored": [{"result": "1"}, {"result": "2"}],
            "observation": {"app": {"stdout": "o" * 5000, "stderr": ""},
                            "tests": None},
            "history": [{"command_result": {"exit_code": 1, "output": "x"}}],
        }
        self.assertEqual(reduce_context(context, 1)["files"], {})
        self.assertEqual(len(reduce_context(context, 1)["explored"]), 2)
        self.assertEqual(len(reduce_context(context, 2)["explored"]), 1)
        smallest = reduce_context(context, 4)
        self.assertEqual(smallest["explored"], [])
        self.assertEqual(len(smallest["observation"]["app"]["stdout"]), 600)
        self.assertEqual(
            smallest["history"], [{"command_result": {"exit_code": 1}}])
        self.assertEqual(context["files"], {"a.c": "x"})

        answer = json.dumps({"action": "give_up", "reason": "Done"})
        wire = (
            "data: " + json.dumps({"choices": [{
                "delta": {"content": answer}, "finish_reason": "stop"}]})
            + "\n\ndata: [DONE]\n\n").encode()
        reply = io.BytesIO(wire)
        reply.headers = {"Content-Type": "text/event-stream"}
        refused = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        with patch.dict(os.environ, {ct.API_KEY_ENV: "test-token"}), patch(
                "agents.urllib.request.urlopen",
                side_effect=[refused, reply]) as request, patch(
                "sys.stderr", io.StringIO()):
            agents = ModelAgents(load_config(ROOT / "config.json"))
            action = agents.propose(context)

        self.assertEqual(action["action"], "give_up")
        sent = [
            json.loads(json.loads(call.args[0].data)["messages"][1]["content"])
            for call in request.call_args_list]
        self.assertEqual(sent[0]["files"], {"a.c": "x"})
        self.assertEqual(sent[1]["files"], {})
        self.assertIn("omitted", sent[1])
        self.assertEqual(agents.requests[0]["http_status"], 403)


if __name__ == "__main__":
    unittest.main()
