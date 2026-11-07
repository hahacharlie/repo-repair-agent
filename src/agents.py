"""Fixer, Judge, and fixed test agents."""

import json
import os
import re
import sys
import threading
import time
import urllib.request

import constants as ct


def basic_run_steps(context):
    # Make simple instructions when an AI response is not available.
    repository = context["repository"]
    steps = []

    if context.get("fixed_files"):
        steps.append(
            "Copy the files from fixed_files into the same paths in your "
            "repository.")
    if repository["language"] != "python":
        packages = context.get("installed_packages", [])
        if packages:
            steps.append("Approved dependencies: " + " ".join(packages))
        dependency = repository.get("dependency_command")
        if dependency:
            steps.append("Fetch dependencies: " + " ".join(dependency))
        steps.append("Build: " + " ".join(repository["build_command"]))
        if repository.get("test_command"):
            steps.append("Run the tests: " + " ".join(repository["test_command"]))
        return "\n".join(["Run these commands from the repository root.", ""] + [
            str(number) + ". " + step
            for number, step in enumerate(steps, start=1)])
    steps.append("Create a virtual environment: python -m venv .venv")
    steps.append(
        "Activate it with .venv\\Scripts\\Activate.ps1 on Windows PowerShell, "
        "or source .venv/bin/activate on macOS and Linux")
    if context.get("has_requirements"):
        steps.append(
            "Install declared dependencies: "
            "python -m pip install -r requirements.txt")
    packages = context.get("installed_packages", [])
    if packages:
        steps.append(
            "Approved packages recorded in requirements.txt: "
            + " ".join(packages))
    steps.append("Run the application: python " + repository["entry"])
    if repository.get("test_command"):
        steps.append(
            "Run the tests: python "
            + " ".join(repository["test_command"]))

    lines = ["Run these commands from the repository root.", ""]
    lines.extend(
        str(number) + ". " + step
        for number, step in enumerate(steps, start=1)
    )
    return "\n".join(lines)


class ModelResponseError(ValueError):
    """An error that does not show the model reply."""


class InvalidModelJSONError(ModelResponseError):
    """The model reply was not one JSON object."""


class AgentTimeLimitError(ModelResponseError):
    """The cumulative Fixer/Judge API time budget was exhausted."""


def summarize_token_usage(requests, usage_field="local_token_usage", source=None):
    """Aggregate token usage across model requests."""
    if not requests:
        return {
            "source": "no_model_requests",
            "available": True,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
    aliases = {
        "prompt_tokens": ("prompt_tokens", "input_tokens"),
        "completion_tokens": ("completion_tokens", "output_tokens"),
        "total_tokens": ("total_tokens",),
    }
    totals = {name: 0 for name in aliases}
    found = {name: False for name in aliases}
    sources = set()
    for request in requests:
        usage = request.get(usage_field)
        if not isinstance(usage, dict):
            continue
        if isinstance(usage.get("source"), str):
            sources.add(usage["source"])
        for name, names in aliases.items():
            value = next((usage.get(key) for key in names
                          if type(usage.get(key)) is int), None)
            if value is not None and value >= 0:
                totals[name] += value
                found[name] = True
    if (not found["total_tokens"]
            and found["prompt_tokens"] and found["completion_tokens"]):
        totals["total_tokens"] = (
            totals["prompt_tokens"] + totals["completion_tokens"])
        found["total_tokens"] = True
    return {
        "source": source or (
            next(iter(sources)) if len(sources) == 1 else "mixed"),
        "available": any(found.values()),
        **{
            name: totals[name] if found[name] else None
            for name in aliases
        },
    }


class TiktokenCounter:
    """Estimate prompt and response tokens without a model download."""

    def __init__(self, encoding=None):
        self.encoding = encoding
        self.load_attempted = encoding is not None
        self.simple_fallback = False

    def get_encoding(self):
        if not self.load_attempted:
            self.load_attempted = True
            try:
                import tiktoken
                self.encoding = tiktoken.get_encoding("cl100k_base")
            except Exception:
                self.simple_fallback = True
        return self.encoding

    def encoding_name(self):
        if self.simple_fallback:
            return "simple_python_fallback"
        return getattr(self.encoding, "name", "cl100k_base")

    def source_name(self):
        return (
            "simple_python_estimate"
            if self.simple_fallback
            else "tiktoken_cl100k_base_estimate"
        )

    def count_text(self, text):
        encoding = self.get_encoding()
        if encoding is not None:
            return len(encoding.encode(text))
        # Keep a number available when tiktoken cannot load its encoding.
        return len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))

    def count_prompt(self, model, messages):
        del model
        text = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        return self.count_text(text)

    def count_response(self, model, text):
        del model
        return self.count_text(text)


def remaining_request_time(deadline, idle_timeout, clock):
    remaining = deadline - clock()
    if remaining <= 0:
        raise AgentTimeLimitError(
            "Fixer/Judge API time limit reached; no action was applied")
    return min(remaining, idle_timeout)


def set_response_timeout(response, timeout):
    # Keep one blocking stream read inside the remaining wall-clock budget.
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is not None:
        sock.settimeout(max(timeout, 0.001))


def parse_model_json(content):
    # Remove one code block when the model adds one.
    text = content.strip()
    if text.startswith("```"):
        import re
        fenced = re.fullmatch(
            r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE)
        if fenced:
            text = fenced.group(1).strip()
    # The final reply must be one JSON object.
    try:
        value = json.loads(text)
    except ValueError:
        raise InvalidModelJSONError(
            "Model returned invalid JSON; no action was applied") from None
    if not isinstance(value, dict):
        raise InvalidModelJSONError(
            "Model response must be a JSON object")
    return value


def read_model_stream(
        response, metrics, start, deadline, idle_timeout, clock,
        show_progress=True):
    # Join the small reply parts sent by the model server.
    content = []
    generated = []
    finished = None
    while True:
        timeout = remaining_request_time(deadline, idle_timeout, clock)
        set_response_timeout(response, timeout)
        line = response.readline()
        remaining_request_time(deadline, idle_timeout, clock)
        if not line:
            break
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        # The server sends DONE after the full reply.
        if payload == b"[DONE]":
            if finished != "stop":
                raise ModelResponseError(
                    "Model response incomplete; finish reason was not stop")
            return "".join(content), "".join(generated)
        try:
            chunk = json.loads(payload)
        except ValueError:
            raise ModelResponseError(
                "Provider returned an invalid stream event") from None
        if "error" in chunk:
            raise ModelResponseError("Provider reported a streaming error")
        # Read only the first answer choice.
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta", {})
            # Save the first time each reply part is seen.
            for field, label in (
                    ("reasoning", "first_reasoning_seconds"),
                    ("reasoning_content", "first_reasoning_seconds"),
                    ("content", "first_content_seconds")):
                if delta.get(field) and label not in metrics:
                    metrics[label] = round(clock() - start, 2)
                    if show_progress:
                        phase = (
                            "answer" if field == "content" else "reasoning")
                        agent_name = metrics["agent"]
                        model_name = metrics["model"]
                        print(
                            f"[{agent_name}: {model_name}] "
                            f"Receiving {phase} ({metrics[label]}s)",
                            file=sys.stderr,
                            flush=True,
                        )
            reasoning = (
                delta.get("reasoning") or delta.get("reasoning_content"))
            if reasoning:
                generated.append(reasoning)
            if delta.get("content"):
                content.append(delta["content"])
                generated.append(delta["content"])
            if choice.get("finish_reason"):
                finished = choice["finish_reason"]
                metrics["finish_reason"] = finished
        if chunk.get("usage"):
            metrics["usage"] = chunk["usage"]
    raise ModelResponseError(
        "Model stream ended without [DONE]; no action was applied")


def read_model_json(response, deadline, idle_timeout, clock):
    # Do not let a non-streaming response evade the cumulative deadline.
    chunks = []
    while True:
        timeout = remaining_request_time(deadline, idle_timeout, clock)
        set_response_timeout(response, timeout)
        chunk = response.read(65536)
        remaining_request_time(deadline, idle_timeout, clock)
        if not chunk:
            return json.loads(b"".join(chunks))
        chunks.append(chunk)


class ModelAgents:
    def __init__(
            self, config, capture_logs=False,
            time_budget_seconds=ct.DEFAULT_AGENT_TIMEOUT, clock=None,
            token_counter=None):
        # Read the key from the local environment.
        self.config = config
        self.requests = []
        self.logs = []
        self.capture_logs = capture_logs
        if (type(time_budget_seconds) is not int
                or not ct.MINIMUM_AGENT_TIMEOUT
                <= time_budget_seconds <= ct.MAXIMUM_AGENT_TIMEOUT):
            raise ValueError("Invalid Fixer/Judge API time limit")
        self.time_budget_seconds = time_budget_seconds
        self.api_time_used_seconds = 0.0
        self.clock = clock or time.monotonic
        self.token_counter = token_counter or TiktokenCounter()
        self.key = os.environ.get(ct.API_KEY_ENV)
        if not self.key:
            raise ValueError(
                "Set " + ct.API_KEY_ENV
                + " or use --script for offline operation")
        # Add the chat path when the base URL is given.
        self.endpoint = config["endpoint"].rstrip("/")
        if not self.endpoint.endswith("/chat/completions"):
            self.endpoint += "/chat/completions"

    def request(
            self, model, instruction, context,
            agent="model", purpose="request", show_progress=True):
        """Retry twice when the model does not return one JSON object."""
        retry_instruction = instruction
        for attempt in range(ct.MODEL_JSON_MAX_ATTEMPTS):
            try:
                return self._request_once(
                    model,
                    retry_instruction,
                    context,
                    agent=agent,
                    purpose=purpose,
                    show_progress=show_progress,
                )
            except InvalidModelJSONError:
                if attempt + 1 == ct.MODEL_JSON_MAX_ATTEMPTS:
                    raise ModelResponseError(
                        "Model returned invalid JSON after "
                        + str(ct.MODEL_JSON_MAX_ATTEMPTS)
                        + " attempts; no action was applied"
                    ) from None
                retry_number = attempt + 1
                if show_progress:
                    print(
                        "Invalid model JSON. Retrying "
                        f"({retry_number}/{ct.MODEL_JSON_MAX_ATTEMPTS - 1})...",
                        file=sys.stderr,
                        flush=True,
                    )
                retry_instruction = instruction + ct.MODEL_JSON_RETRY_INSTRUCTION

    def _request_once(
            self, model, instruction, context,
            agent="model", purpose="request",
            response_parser=parse_model_json, user_content=None,
            show_progress=True):
        remaining_budget = (
            self.time_budget_seconds - self.api_time_used_seconds)
        if remaining_budget <= 0:
            raise AgentTimeLimitError(
                "Fixer/Judge API time limit reached; no action was applied")

        # Send the rule and repo data as two separate messages.
        messages = [
            {"role": "system", "content": instruction},
            {
                "role": "user",
                "content": (
                    user_content
                    if user_content is not None
                    else json.dumps(context)
                ),
            },
        ]
        local_usage = {
            "source": "tiktoken_cl100k_base_estimate",
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }
        try:
            prompt_tokens = self.token_counter.count_prompt(model, messages)
            source_name = getattr(self.token_counter, "source_name", None)
            if callable(source_name):
                local_usage["source"] = source_name()
            local_usage["encoding"] = self.token_counter.encoding_name()
            local_usage["prompt_tokens"] = prompt_tokens
            local_usage["completion_tokens"] = 0
            local_usage["total_tokens"] = prompt_tokens
        except Exception as error:
            local_usage["error_type"] = type(error).__name__
        body = json.dumps({
            "model": model,
            "temperature": 0,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": messages,
        }).encode()
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Authorization": "Bearer " + self.key,
                "Content-Type": "application/json",
            },
        )
        # Keep simple time and token data for the run report.
        start = self.clock()
        deadline = start + remaining_budget
        api_end = None
        metrics = {
            "agent": agent,
            "model": model,
            "request_bytes": len(body),
            "agent_time_budget_seconds": self.time_budget_seconds,
            "local_token_usage": local_usage,
        }
        self.requests.append(metrics)
        log = None
        if self.capture_logs:
            log = {
                "run": len(self.logs) + 1,
                "agent": agent,
                "purpose": purpose,
                "model": model,
                "prompt": {
                    "system": instruction,
                    **(
                        {"user": user_content}
                        if user_content is not None
                        else {"context": json.loads(json.dumps(context))}
                    ),
                },
                "metrics": metrics,
            }
            self.logs.append(log)
        stopped = threading.Event()

        def progress():
            # Show a short note while a model call is still running.
            while not stopped.wait(ct.MODEL_PROGRESS_INTERVAL_SECONDS):
                elapsed = self.clock() - start
                print(
                    f"[{agent}: {model}] Request in progress ({elapsed:.0f}s)",
                    file=sys.stderr,
                    flush=True,
                )

        worker = None
        if show_progress:
            print(
                f"[{agent}: {model}] Request started",
                file=sys.stderr,
                flush=True,
            )
            worker = threading.Thread(target=progress, daemon=True)
            worker.start()
        try:
            idle_timeout = self.config.get(
                "api_timeout_seconds", ct.DEFAULT_API_TIMEOUT)
            # Read either a normal JSON reply or a streamed reply.
            with urllib.request.urlopen(
                    request,
                    timeout=min(idle_timeout, remaining_budget)) as response:
                metrics["headers_seconds"] = round(self.clock() - start, 2)
                metrics["request_id"] = response.headers.get(
                    "x-obleth-request-id")
                if "application/json" in response.headers.get(
                        "Content-Type", ""):
                    result = read_model_json(
                        response, deadline, idle_timeout, self.clock)
                    choice = result["choices"][0]
                    metrics["finish_reason"] = choice.get("finish_reason")
                    if choice.get("finish_reason") != "stop":
                        raise ModelResponseError(
                            "Model response incomplete; finish reason was not stop")
                    message = choice["message"]
                    content = message["content"]
                    reasoning = (
                        message.get("reasoning")
                        or message.get("reasoning_content") or "")
                    generated = reasoning + content
                else:
                    content, generated = read_model_stream(
                        response,
                        metrics,
                        start,
                        deadline,
                        idle_timeout,
                        self.clock,
                        show_progress=show_progress,
                    )
            api_end = self.clock()
            metrics["content_chars"] = len(content)
            try:
                completion_tokens = self.token_counter.count_response(
                    model, generated)
                local_usage["completion_tokens"] = completion_tokens
                prompt_tokens = local_usage["prompt_tokens"]
                if prompt_tokens is not None:
                    local_usage["total_tokens"] = (
                        prompt_tokens + completion_tokens)
            except Exception as error:
                local_usage.setdefault("error_type", type(error).__name__)
            if log is not None:
                log["response_text"] = content
            parsed = response_parser(content)
            if log is not None:
                log["response"] = parsed
            return parsed
        except Exception as error:
            metrics["error_type"] = type(error).__name__
            if log is not None:
                log["error_type"] = type(error).__name__
            raise
        finally:
            end = api_end if api_end is not None else self.clock()
            elapsed = max(0.0, end - start)
            self.api_time_used_seconds = min(
                self.time_budget_seconds,
                self.api_time_used_seconds + elapsed,
            )
            metrics["elapsed_seconds"] = round(elapsed, 2)
            metrics["agent_time_used_seconds"] = round(
                self.api_time_used_seconds, 2)
            metrics["agent_time_remaining_seconds"] = round(
                self.time_budget_seconds - self.api_time_used_seconds, 2)
            stopped.set()
            if worker is not None:
                worker.join()
                elapsed_text = metrics["elapsed_seconds"]
                print(
                    f"[{agent}: {model}] Request ended ({elapsed_text}s)",
                    file=sys.stderr,
                    flush=True,
                )

    def request_text(
            self, model, instruction, user_message,
            agent="model", purpose="request"):
        """Return a plain-text model reply for a legacy baseline."""
        return self._request_once(
            model,
            instruction,
            {},
            agent=agent,
            purpose=purpose,
            response_parser=lambda content: content.strip(),
            user_content=user_message,
        )

    def plan_commands(self, context):
        """Ask the Judge for commands evidenced by bounded root context."""
        instruction = ct.COMMAND_PLANNING_INSTRUCTION
        response = self.request(
            self.config["judge_model"],
            instruction,
            context,
            agent="judge",
            purpose="repository_commands",
            show_progress=False,
        )
        fields = {
            "dependency_command", "build_command", "test_command"}
        if not isinstance(response, dict) or set(response) != fields:
            raise ValueError("Invalid repository command plan")
        for name in fields:
            command = response[name]
            if command is None:
                continue
            if (not isinstance(command, list) or not command
                    or len(command) > ct.COMMAND_MAX_ARGUMENTS
                    or any(not isinstance(item, str) or not item
                           or len(item) > ct.COMMAND_MAX_ARGUMENT_CHARS
                           for item in command)):
                raise ValueError("Invalid repository command: " + name)
        if response["build_command"] is None:
            raise ValueError("README does not document a build command")
        return response

    def propose(self, context):
        # Ask the Fixer for one install, edit, or stop action.
        instruction = ct.FIXER_REPAIR_INSTRUCTION
        return self.request(
            self.config["fixer_model"], instruction, context,
            agent="fixer", purpose="repair_proposal")

    def review(self, context):
        # Ask the Judge to accept or reject the code change.
        instruction = ct.JUDGE_REVIEW_INSTRUCTION
        response = self.request(
            self.config["judge_model"], instruction, context,
            agent="judge", purpose="code_review")
        if (not isinstance(response, dict)
                or type(response.get("approved")) is not bool
                or not isinstance(response.get("reason"), str)):
            raise ValueError("Invalid judge response")
        return response

    def generate_run_steps(self, context):
        # Ask the Fixer for instructions that work outside the temporary copy.
        instruction = ct.RUN_STEPS_INSTRUCTION
        response = self.request(
            self.config["fixer_model"], instruction, context,
            agent="fixer", purpose="run_instructions")
        if (not isinstance(response, dict)
                or set(response) != {"steps"}
                or not isinstance(response["steps"], str)
                or not response["steps"].strip()
                or len(response["steps"]) > ct.RUN_STEPS_MAX_CHARS):
            raise ValueError("Invalid run instructions")
        return response["steps"].strip()


class ScriptedAgents:
    """Use fixed actions for tests instead of calling a model."""

    def __init__(self, steps):
        self.steps = iter(steps)
        self.current = {}

    def propose(self, context):
        del context
        # Return the next saved action from the test file.
        self.current = next(
            self.steps,
            {"action": {"action": "give_up", "reason": "Script exhausted"}},
        )
        return self.current["action"]

    def review(self, context):
        del context
        # Return the saved yes or no Judge answer.
        value = self.current.get("approved", False)
        if type(value) is not bool:
            raise ValueError("Script approval must be boolean")
        return {"approved": value, "reason": "Scripted review"}

    def generate_run_steps(self, context):
        # Offline tests use fixed instructions instead of an AI call.
        return basic_run_steps(context)
