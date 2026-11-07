from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents import (
    AgentTimeLimitError, ModelAgents, ModelResponseError, TiktokenCounter,
    parse_model_json, summarize_token_usage,
)
from config import load_config
import constants as ct


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.json"


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class TimedStream(io.BytesIO):
    def __init__(self, wire, clock, seconds_per_read):
        super().__init__(wire)
        self.clock = clock
        self.seconds_per_read = seconds_per_read
        self.headers = {
            "Content-Type": "text/event-stream",
            "x-obleth-request-id": "timed-request",
        }

    def readline(self, size=-1):
        line = super().readline(size)
        self.clock.value += self.seconds_per_read
        return line


def timed_stream_response(content, clock, seconds_per_read):
    chunk = {"choices": [{
        "delta": {"content": json.dumps(content)},
        "finish_reason": "stop",
    }]}
    wire = (
        "data: " + json.dumps(chunk) + "\n"
        "data: [DONE]\n"
    ).encode()
    return TimedStream(wire, clock, seconds_per_read)


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


class FakeEncoding:
    name = "test_encoding"

    def encode(self, text):
        return list(text.encode())


class FakeTokenCounter:
    def __init__(self):
        self.responses = []

    def encoding_name(self):
        return "test_encoding"

    def count_prompt(self, model, messages):
        return 11

    def count_response(self, model, text):
        self.responses.append(text)
        return 7


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.token_counter = FakeTokenCounter()
        counter_patch = patch(
            "agents.TiktokenCounter", return_value=self.token_counter)
        counter_patch.start()
        self.addCleanup(counter_patch.stop)

    def test_tokens_are_estimated_locally_with_tiktoken(self):
        counter = TiktokenCounter(encoding=FakeEncoding())
        messages = [{"role": "user", "content": "hello"}]

        self.assertGreater(counter.count_prompt(
            "qwen3-coder-30b-a3b-instruct", messages), 5)
        self.assertEqual(counter.count_response(
            "qwen3-coder-30b-a3b-instruct", "reply"), 5)
        self.assertEqual(counter.encoding_name(), "test_encoding")

        usage = summarize_token_usage([
            {"local_token_usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            }},
            {"local_token_usage": {
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "total_tokens": 8,
            }},
        ])
        self.assertEqual(usage, {
            "source": "mixed",
            "available": True,
            "prompt_tokens": 16,
            "completion_tokens": 10,
            "total_tokens": 26,
        })

        self.assertEqual(summarize_token_usage([]), {
            "source": "no_model_requests",
            "available": True,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        })

    def test_token_count_has_a_simple_fallback(self):
        counter = TiktokenCounter()
        with patch("tiktoken.get_encoding", side_effect=OSError("offline")):
            count = counter.count_response("model", "hello, world")

        self.assertEqual(count, 3)
        self.assertEqual(counter.encoding_name(), "simple_python_fallback")
        self.assertEqual(counter.source_name(), "simple_python_estimate")

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
            self.assertIn("Create .venv", agents.generate_run_steps({}))
        for call, model in zip(
                request.call_args_list,
                ("custom-fixer", "custom-judge", "custom-fixer")):
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
        self.assertIn("context.policy_findings", prompts[1])
        self.assertIn("copying fixed_files the first step", prompts[2])
        self.assertEqual(agents.requests[0]["request_id"], "test-request")
        self.assertNotIn("private reasoning", json.dumps(agents.requests))
        self.assertEqual(
            agents.requests[0]["local_token_usage"]["total_tokens"], 18)
        self.assertIn("private reasoning", self.token_counter.responses[0])
        self.assertEqual(
            [(item["agent"], item["purpose"]) for item in agents.logs],
            [
                ("fixer", "repair_proposal"),
                ("judge", "code_review"),
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

    def test_cumulative_api_budget_excludes_time_between_requests(self):
        config = load_config(CONFIG)
        clock = FakeClock()
        replies = [
            timed_stream_response(
                {"action": "give_up", "reason": "Done"}, clock, 2),
            timed_stream_response(
                {"approved": True, "reason": "Valid"}, clock, 2),
        ]
        with patch.dict(
                "os.environ", {ct.API_KEY_ENV: "test-token"}), patch(
                "agents.urllib.request.urlopen",
                side_effect=replies) as request:
            agents = ModelAgents(
                config, time_budget_seconds=5, clock=clock)
            self.assertEqual(agents.propose({})["action"], "give_up")
            self.assertEqual(agents.api_time_used_seconds, 4)

            # Time outside a model request represents Docker/build work.
            clock.value = 1004
            with self.assertRaisesRegex(
                    AgentTimeLimitError, "API time limit reached"):
                agents.review({})
            with self.assertRaises(AgentTimeLimitError):
                agents.propose({})

        self.assertEqual(request.call_count, 2)
        self.assertEqual(agents.api_time_used_seconds, 5)
        self.assertEqual(
            [call.kwargs["timeout"] for call in request.call_args_list],
            [5, 1],
        )
        self.assertEqual(
            agents.requests[-1]["agent_time_remaining_seconds"], 0)
        self.assertEqual(
            agents.requests[-1]["error_type"], "AgentTimeLimitError")

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

    def test_invalid_json_is_retried_twice_then_succeeds(self):
        config = load_config(CONFIG)
        replies = [
            stream_response("not json"),
            stream_response('{"approved":'),
            stream_response('{"approved": true, "reason": "Valid"}'),
        ]
        with patch.dict(
                "os.environ", {ct.API_KEY_ENV: "test-token"}), patch(
                "agents.urllib.request.urlopen",
                side_effect=replies) as request:
            agents = ModelAgents(config, capture_logs=True)
            result = agents.review({})

        self.assertTrue(result["approved"])
        self.assertEqual(request.call_count, 3)
        self.assertEqual(len(agents.requests), 3)
        retry_prompts = [
            json.loads(call.args[0].data)["messages"][0]["content"]
            for call in request.call_args_list[1:]
        ]
        self.assertTrue(all(
            "previous response was invalid" in prompt
            for prompt in retry_prompts
        ))

    def test_invalid_json_stops_after_three_attempts(self):
        config = load_config(CONFIG)
        with patch.dict(
                "os.environ", {ct.API_KEY_ENV: "test-token"}), patch(
                "agents.urllib.request.urlopen",
                side_effect=[stream_response("not json") for _ in range(3)],
                ) as request:
            agents = ModelAgents(config)
            with self.assertRaisesRegex(
                    ModelResponseError, "invalid JSON after 3 attempts"):
                agents.review({})

        self.assertEqual(request.call_count, 3)

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

    def test_readme_command_plan_uses_judge_without_console_noise(self):
        config = load_config(CONFIG)
        plan = {
            "dependency_command": ["mage", "-version"],
            "build_command": ["mage", "hugo"],
            "test_command": ["mage", "-v", "check"],
        }
        stderr = io.StringIO()
        with patch.dict(
                os.environ, {ct.API_KEY_ENV: "test-token"}), patch(
                "agents.urllib.request.urlopen",
                return_value=stream_response(json.dumps(plan))) as request, (
                redirect_stderr(stderr)):
            agents = ModelAgents(config)
            self.assertEqual(
                agents.plan_commands({
                    "readme": "README build instructions",
                    "root_files": {"Makefile": "build:\n\tproject build\n"},
                }),
                plan,
            )

        self.assertEqual(stderr.getvalue(), "")
        body = json.loads(request.call_args.args[0].data)
        self.assertEqual(body["model"], config["judge_model"])
        self.assertEqual(
            json.loads(body["messages"][1]["content"]),
            {
                "readme": "README build instructions",
                "root_files": {"Makefile": "build:\n\tproject build\n"},
            },
        )
        self.assertEqual(agents.requests[0]["agent"], "judge")


if __name__ == "__main__":
    unittest.main()
