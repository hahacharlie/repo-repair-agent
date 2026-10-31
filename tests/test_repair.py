import tempfile
import shutil
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from repair import (ModelAgents, ModelResponseError, ScriptedAgents, load_config,
                    parse_model_json, repair, run_command, validate_action)


FIXTURE = Path(__file__).resolve().parents[1] / "issue_repos/runtime_error"
FIX = 'def greet(name):\n    return "Hello, " + name\n'


def step(approved=True):
    return {"action": {"action": "edit", "reason": "Fix undefined variable",
                       "files": {"greeting.py": FIX}}, "approved": approved}


def stream_response(content, finish="stop", done=True):
    chunks = [{"choices": [{"delta": {"reasoning": "private reasoning"}}]},
              {"choices": [{"delta": {"content": content[:10]}}]},
              {"choices": [{"delta": {"content": content[10:]}, "finish_reason": finish}]}]
    wire = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
    if done:
        wire += "data: [DONE]\n\n"
    response = io.BytesIO(wire.encode())
    response.headers = {"Content-Type": "text/event-stream", "x-obleth-request-id": "test-request"}
    return response


class RepairTests(unittest.TestCase):
    def test_configured_models_endpoint_and_credentials_reach_requests(self):
        config = {"endpoint": "https://models.example.test/v1/chat/completions",
                  "api_key_env": "TEST_MODEL_KEY", "fixer_model": "custom-fixer",
                  "judge_model": "custom-judge"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            path.write_text(json.dumps(config))
            loaded = load_config(path)
        responses = [stream_response("```json\n" + json.dumps(value) + "\n```")
                     for value in ({"action": "give_up", "reason": "Done"},
                                   {"approved": False, "reason": "Unsafe patch"})]
        with patch.dict("os.environ", {"TEST_MODEL_KEY": "test-token"}, clear=True), patch(
                "repair.urllib.request.urlopen", side_effect=responses) as request:
            agents = ModelAgents(loaded)
            self.assertEqual(agents.propose({})["action"], "give_up")
            self.assertFalse(agents.review({})["approved"])
        for call, model in zip(request.call_args_list, ("custom-fixer", "custom-judge")):
            outbound = call.args[0]
            self.assertEqual(outbound.full_url, config["endpoint"])
            self.assertEqual(json.loads(outbound.data)["model"], model)
            self.assertEqual(outbound.get_header("Authorization"), "Bearer test-token")
            self.assertTrue(json.loads(outbound.data)["stream"])
            self.assertEqual(call.kwargs["timeout"], 300)
        self.assertEqual(agents.requests[0]["request_id"], "test-request")
        self.assertNotIn("private reasoning", json.dumps(agents.requests))

    def test_stream_rejects_truncation_and_uses_custom_timeout(self):
        config = load_config(FIXTURE.parents[1] / "config.json")
        config["api_timeout_seconds"] = 450
        for finish, done in (("length", True), ("stop", False), ("content_filter", True)):
            with self.subTest(finish=finish, done=done), patch.dict(
                    "os.environ", {config["api_key_env"]: "test-token"}), patch(
                    "repair.urllib.request.urlopen", return_value=stream_response(
                        '{"approved": true, "reason": "ok"}', finish, done)) as request:
                agents = ModelAgents(config)
                with self.assertRaises(ModelResponseError):
                    agents.review({})
                self.assertEqual(request.call_args.kwargs["timeout"], 450)
                self.assertEqual(agents.requests[0]["error_type"], "ModelResponseError")

    def test_json_parser_accepts_fences_but_rejects_prose(self):
        value = {"approved": False, "reason": "Keep behavior"}
        for text in (json.dumps(value), "```json\n" + json.dumps(value) + "\n```",
                     "```\n" + json.dumps(value) + "\n```"):
            self.assertEqual(parse_model_json(text), value)
        for text in ("Here is the answer: " + json.dumps(value), "[]", "", '{"approved":'):
            with self.subTest(text=text), self.assertRaises(ModelResponseError):
                parse_model_json(text)

    def test_nonstreaming_provider_fallback(self):
        config = load_config(FIXTURE.parents[1] / "config.json")
        payload = {"choices": [{"finish_reason": "stop", "message": {
            "content": '{"approved": false, "reason": "Reject"}'}}]}
        response = io.BytesIO(json.dumps(payload).encode())
        response.headers = {"Content-Type": "application/json"}
        with patch.dict("os.environ", {config["api_key_env"]: "test-token"}), patch(
                "repair.urllib.request.urlopen", return_value=response):
            self.assertFalse(ModelAgents(config).review({})["approved"])

    def test_timeout_is_reported_without_exposing_provider_message(self):
        config = load_config(FIXTURE.parents[1] / "config.json")
        with patch.dict("os.environ", {config["api_key_env"]: "test-token"}), patch(
                "repair.urllib.request.urlopen", side_effect=TimeoutError("private provider detail")):
            report = repair(FIXTURE, ModelAgents(config))
        self.assertEqual(report["status"], "failed")
        self.assertIn("TimeoutError", report["reason"])
        self.assertEqual(report["model_requests"][0]["error_type"], "TimeoutError")
        self.assertNotIn("private provider detail", json.dumps(report))
        self.assertEqual(report["diff"], "")

    def test_config_rejects_mistakes_before_api_calls(self):
        config = load_config(FIXTURE.parents[1] / "config.json")
        invalid = [[], {}, dict(config, fixer_model=""), dict(config, judge_model=4),
                   dict(config, unexpected="typo"), dict(config, endpoint="http://example.test"),
                   dict(config, endpoint="https://user:secret@example.test"),
                   dict(config, api_key_env="invalid-key-name"),
                   *[dict(config, api_timeout_seconds=value) for value in (0, -1, True, "300", 3601)]]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            for value in invalid:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    path.write_text(json.dumps(value))
                    load_config(path)
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(ValueError):
            ModelAgents(config)

    def test_api_base_url_is_normalized(self):
        config = load_config(FIXTURE.parents[1] / "config.json")
        with patch.dict("os.environ", {config["api_key_env"]: "test-token"}):
            for endpoint in ("https://example.test/v1", "https://example.test/v1/",
                             "https://example.test/v1/chat/completions"):
                with self.subTest(endpoint=endpoint):
                    agents = ModelAgents(dict(config, endpoint=endpoint))
                    self.assertEqual(agents.endpoint, "https://example.test/v1/chat/completions")

    def test_uv_installs_into_target_environment(self):
        installations = []

        def execute(command, cwd, timeout):
            if command[:3] == ["uv", "pip", "install"]:
                installations.append(command)
                return {"exit_code": 0, "timeout": False, "stdout": "", "stderr": ""}
            return run_command(command, cwd, timeout)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            shutil.copytree(FIXTURE, root)
            (root / "requirements.txt").write_text("example-package\n")
            install = {"action": {"action": "install", "package": "another-package",
                                  "reason": "Exercise target installation"}}
            with patch("repair.run_command", side_effect=execute):
                report = repair(root, ScriptedAgents([install, step()]), allow_install=True)
            self.assertEqual(report["status"], "passed")
            self.assertEqual(len(installations), 2)
            self.assertEqual(installations[0][3], "--python")
            self.assertEqual(installations[0][4], installations[1][4])
            self.assertIn("env", Path(installations[0][4]).parts)
            self.assertEqual(installations[0][5:], ["-r", "requirements.txt"])
            self.assertEqual(installations[1][5:], ["another-package"])

    def test_approved_repair_passes_and_original_is_unchanged(self):
        original = (FIXTURE / "greeting.py").read_bytes()
        report = repair(FIXTURE, ScriptedAgents([step()]))
        self.assertEqual(report["status"], "passed")
        self.assertIn("+    return", report["diff"])
        self.assertEqual((FIXTURE / "greeting.py").read_bytes(), original)

    def test_rejected_patch_never_applies_and_limit_stops(self):
        report = repair(FIXTURE, ScriptedAgents([step(False)]), rounds=1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["diff"], "")
        self.assertEqual(len(report["history"]), 1)

    def test_invalid_paths_and_test_edits_are_rejected(self):
        for name in ("../outside.py", "/tmp/outside.py", "tests/test_greeting.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_action({"action": "edit", "reason": "bad", "files": {name: ""}}, FIXTURE)

    def test_nested_action_is_rejected_cleanly(self):
        for action in ({"type": "edit"}, ["edit"], None, 42):
            with self.subTest(action=action), self.assertRaises(ValueError):
                validate_action({"action": action, "reason": "Invalid schema"}, FIXTURE)

    def test_package_arguments_cannot_inject_options(self):
        for package in ("--upgrade", "thing;whoami", "https://example.com/x", "x y"):
            with self.subTest(package=package), self.assertRaises(ValueError):
                validate_action({"action": "install", "reason": "bad", "package": package}, FIXTURE)

    def test_regression_is_rolled_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text("print('ok')\n")
            (root / "test_main.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_failure(self): self.fail('preexisting')\n")
            action = {"action": {"action": "edit", "reason": "harmful", "files": {"main.py": "raise RuntimeError('regression')\n"}}, "approved": True}
            report = repair(root, ScriptedAgents([action]), rounds=1)
            self.assertTrue(report["history"][0]["rolled_back"])
            self.assertEqual(report["diff"], "")


if __name__ == "__main__":
    unittest.main()
