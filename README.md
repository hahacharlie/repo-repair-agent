# Repository Repair Agent

An initial Python implementation of the capstone design documented in the parent workspace’s `../document/` directory. It detects a Python entry point, copies a repository into a temporary directory, creates a fresh virtual environment, executes the application and detected unittest tests, and iterates through Fixer proposals and independent Judge reviews. Original repository files remain unchanged.

## Requirements and quick start

Install uv and Python 3.10+. The project uses `pyproject.toml` and `uv.lock` to manage its environment, with no third-party Python dependencies. Run from this directory:

```sh
uv sync --locked
uv run repair.py issue_repos/runtime_error --script issue_repos/runtime_error/repair.json
uv run python -m unittest discover -s tests -v
```

The offline example repairs an undefined variable and checks greeting behavior. Its scripted approval is a deterministic integration demonstration, not evidence of model judgment quality. Outputs are `results/run.json` and `results/run.patch`. CLI exit codes: 0 for passing execution, 1 for unsuccessful repair, 2 for setup errors. `execution_only` explicitly indicates that no tests were found.

## Model-backed operation

Set `ASU_AIR_API_KEY` in your environment, then run:

```sh
uv run repair.py /path/to/trusted/python-repository --rounds 5 --timeout 30
```

### Configure agent models

Edit `config.json` to select the Fixer and Judge models:

```json
{
  "endpoint": "https://openai.rc.asu.edu/v1/chat/completions",
  "api_key_env": "ASU_AIR_API_KEY",
  "fixer_model": "qwen3-coder-30b-a3b-instruct",
  "judge_model": "qwen3-235b-a22b-instruct-2507",
  "api_timeout_seconds": 300
}
```

`endpoint` is an HTTPS API base URL (such as `https://openai.rc.asu.edu/v1`) or a complete chat-completions URL, shared by both agents. The client appends `/chat/completions` to base URLs. `api_key_env` names the environment variable containing its bearer token; never put the token itself in this file. Model names must be available through the selected endpoint. Use different Fixer and Judge models for independent review. The Main Orchestrator is not yet model-driven and has no model setting.

Use a separate configuration with:

```sh
uv run repair.py /path/to/trusted/python-repository --config /path/to/models.json
```

By default, the runner loads `config.json` beside `repair.py`, regardless of the current working directory. The four endpoint/model/key fields are required. `api_timeout_seconds` is optional and defaults to 300 (integer, 1–3600). It controls socket waits, including the wait for initial response headers; it is not a total wall-clock deadline for an active stream. The CLI `--timeout` separately limits each target execution or installation command. Unknown fields are rejected. Offline `--script` mode does not load model configuration or require a key.

The configuration selects ASU Voyager. Requests enable SSE streaming with usage reporting. Progress goes to stderr every 15 seconds while waiting and when reasoning or answer generation begins; model reasoning and answer text are not printed. The run report records per-request timing, request IDs, finish reasons, and token usage when provided. A server that returns a regular JSON completion instead is also accepted.

Answers must contain a JSON object, either plain or wrapped in a single Markdown code fence. Prose surrounding JSON, malformed JSON, truncated replies, and streams missing `[DONE]` stop the run without applying that response. Code edits require Judge approval and may change multiple existing Python source files. Test edits, path traversal, and repeated actions are rejected. Regressions in previously passing execution or tests trigger rollback.

Temporary target environments are created with `uv venv` using the runner's Python interpreter. Dependency installation is disabled by default. `--allow-install` enables declared `requirements.txt` installation and Fixer-requested plain package names through `uv pip install --python <target-python>` inside the temporary environment. Installation does not require Judge approval. Use `uv add <package>` for runner dependencies; target dependencies remain isolated from the runner.

## Current boundaries

This is the first implementation slice, not the complete milestone system. It supports root `main.py`, `app.py`, or `run.py` and standard-library unittest discovery (`test*.py`, under `tests/` when present). Pytest-only projects and other languages are not supported. Detected tests are not a guarantee of adequate functional coverage.

A temporary directory and virtual environment are **not a security sandbox**. Target programs and package installers run with your user permissions; use only trusted fixtures. Relevant source files and execution output are sent to Voyager in live mode. Do not use repositories containing secrets or private data. Reports contain source edits and command output; review before sharing.

Next milestones: Docker isolation and image selection; model-driven orchestration and context selection; broader project detection; validated GitHub PR publishing; benchmark tasks, hidden functional checks, labeled Judge patches, and comparisons against the two-agent and single-call baselines. The current loop is deterministic orchestration and has no Main Orchestrator model. No PRs are created or pushed.

## Layout and contribution

- `repair.py`: inspection, execution, validation, agent clients, repair loop, CLI.
- `config.json`: model names, shared API endpoint, and credential environment-variable name.
- `pyproject.toml`, `uv.lock`, `.python-version`: uv project metadata, dependency lock, and development Python version.
- `issue_repos/runtime_error/`: deliberately broken repository, protected functional tests, offline repair script.
- `tests/`: runner integration and validation checks.

Use four-space Python indentation, descriptive snake_case names, and standard-library unittest. Run the test command above before submitting changes. Describe behavior changes, validation, and scope limitations in pull requests. Never commit API keys, local environments, or private run logs.
