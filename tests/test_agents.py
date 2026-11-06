import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents import ModelAgents, ModelResponseError, parse_model_json
from config import load_config
import constants as ct


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.json"


def stream_response(content, finish="stop", done=True):
    chunks = [
        {"choices": [{"delta": {"reasoning": "private reasoning"}}]},
        {"choices": [{"delta": {"content": content[:10]}}]},
        {"choices": [{
            "delta": {"content": content[10:]},
            "finish_reason": finish,
        }]},
    ]
    wire = "".join(
        "data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
    if done:
        wire += "data: [DONE]\n\n"
    response = io.BytesIO(wire.encode())
    response.headers = {
        "Content-Type": "text/event-stream",
        "x-obleth-request-id": "test-request",
    }
    return response


class AgentTests(unittest.TestCase):
    def test_configured_models_endpoint_and_credentials_reach_requests(self):
        config = {
            "endpoint": "https://models.example.test/v1/chat/completions",
            "fixer_model": "custom-fixer",
            "judge_model": "custom-judge",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            path.write_text(json.dumps(config))
            loaded = load_config(path)
        responses = [
            stream_response("```json\n" + json.dumps(value) + "\n```")
            for value in (
                {"action": "give_up", "reason": "Done"},
                {"approved": False, "reason": "Unsafe patch"},
                {"approved": True, "reason": "Needed package"},
                {"approved": True, "reason": "Stopping is justified"},
                {"steps": "1. Create .venv\n2. Run the app"},
            )
        ]
        with patch.dict(
                "os.environ", {ct.API_KEY_ENV: "test-token"}, clear=True), patch(
                "agents.urllib.request.urlopen",
                side_effect=responses) as request:
            agents = ModelAgents(loaded, capture_logs=True)
            self.assertEqual(agents.propose({})["action"], "give_up")
            self.assertFalse(agents.review({})["approved"])
            self.assertTrue(agents.review_install({})["approved"])
            self.assertTrue(agents.review_give_up({})["approved"])
            self.assertIn("Create .venv", agents.generate_run_steps({}))
        for call, model in zip(
                request.call_args_list,
                ("custom-fixer", "custom-judge", "custom-judge", "custom-judge", "custom-fixer")):
            outbound = call.args[0]
            self.assertEqual(outbound.full_url, config["endpoint"])
            self.assertEqual(json.loads(outbound.data)["model"], model)
            self.assertEqual(
                outbound.get_header("Authorization"), "Bearer test-token")
            self.assertTrue(json.loads(outbound.data)["stream"])
            self.assertEqual(call.kwargs["timeout"], 300)
        prompts = [
            json.loads(call.args[0].data)["messages"][0]["content"]
            for call in request.call_args_list
        ]
        self.assertIn("capabilities.dependency_installation", prompts[0])
        self.assertIn("records it in the project manifest", prompts[0])
        self.assertIn("Preserve every unrelated line exactly", prompts[0])
        self.assertIn("Reject the entire patch", prompts[1])
        self.assertIn("installation.supported", prompts[2])
        self.assertIn("context.policy_findings", prompts[1])
        self.assertIn("proposal to stop", prompts[3])
        self.assertIn("copying fixed_files the first step", prompts[4])
        self.assertEqual(agents.requests[0]["request_id"], "test-request")
        self.assertNotIn("private reasoning", json.dumps(agents.requests))
        self.assertEqual(
            [(item["agent"], item["purpose"]) for item in agents.logs],
            [
                ("fixer", "repair_proposal"),
                ("judge", "code_review"),
                ("judge", "dependency_review"),
                ("judge", "give_up_review"),
                ("fixer", "run_instructions"),
            ],
        )
        self.assertIn("system", agents.logs[0]["prompt"])
        self.assertEqual(
            agents.logs[0]["response"],
            {"action": "give_up", "reason": "Done"},
        )
        self.assertIn("response_text", agents.logs[0])
        self.assertNotIn("test-token", json.dumps(agents.logs))
        self.assertNotIn("private reasoning", json.dumps(agents.logs))

    def test_stream_rejects_truncation_and_uses_custom_timeout(self):
        config = load_config(CONFIG)
        config["api_timeout_seconds"] = 450
        for finish, done in (
                ("length", True), ("stop", False), ("content_filter", True)):
            with self.subTest(finish=finish, done=done), patch.dict(
                    "os.environ", {ct.API_KEY_ENV: "test-token"}), patch(
                    "agents.urllib.request.urlopen",
                    return_value=stream_response(
                        '{"approved": true, "reason": "ok"}', finish, done),
                    ) as request:
                agents = ModelAgents(config, capture_logs=True)
                with self.assertRaises(ModelResponseError):
                    agents.review({})
                self.assertEqual(request.call_args.kwargs["timeout"], 450)
                self.assertEqual(
                    agents.requests[0]["error_type"], "ModelResponseError")
                self.assertEqual(
                    agents.logs[0]["error_type"], "ModelResponseError")

    def test_json_parser_accepts_fences_but_rejects_prose(self):
        value = {"approved": False, "reason": "Keep behavior"}
        for text in (
                json.dumps(value),
                "```json\n" + json.dumps(value) + "\n```",
                "```\n" + json.dumps(value) + "\n```"):
            self.assertEqual(parse_model_json(text), value)
        for text in (
                "Here is the answer: " + json.dumps(value),
                "[]", "", '{"approved":'):
            with self.subTest(text=text), self.assertRaises(ModelResponseError):
                parse_model_json(text)

    def test_nonstreaming_provider_fallback(self):
        config = load_config(CONFIG)
        payload = {"choices": [{
            "finish_reason": "stop",
            "message": {
                "content": '{"approved": false, "reason": "Reject"}',
            },
        }]}
        response = io.BytesIO(json.dumps(payload).encode())
        response.headers = {"Content-Type": "application/json"}
        with patch.dict(
                "os.environ", {ct.API_KEY_ENV: "test-token"}), patch(
                "agents.urllib.request.urlopen",
                return_value=response):
            agents = ModelAgents(config)
            self.assertFalse(agents.review({})["approved"])
            self.assertEqual(agents.logs, [])

    def test_api_base_url_is_normalized(self):
        config = load_config(CONFIG)
        with patch.dict(
                os.environ, {ct.API_KEY_ENV: "test-token"}):
            for endpoint in (
                    "https://example.test/v1",
                    "https://example.test/v1/",
                    "https://example.test/v1/chat/completions"):
                with self.subTest(endpoint=endpoint):
                    agents = ModelAgents(dict(config, endpoint=endpoint))
                    self.assertEqual(
                        agents.endpoint,
                        "https://example.test/v1/chat/completions",
                    )


if __name__ == "__main__":
    unittest.main()
