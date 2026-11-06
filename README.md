# Repository Repair Agent

An initial Python implementation of the repository-repair capstone design. It finds a Python start file, copies the repository into a temporary folder, creates a Python environment inside the selected broken repository, runs the app and its unittest tests, and works through Fixer proposals and Judge reviews. The original source files stay unchanged.

## Requirements and quick start

The commands below use the same form on Windows, macOS, and Linux. If your system uses `python3` instead of `python`, use `python3` in the setup command.

Install Python 3.10 or newer. Run this one time from the project folder:

```console
python -m pip install -r requirements.txt
```

Run the offline sample without an API key:

```console
uv run repo-repair-agent undefined_greeting_variable
```

The offline example repairs an undefined variable and checks greeting behavior. Its scripted approval is a fixed test run, not proof that a model Judge works well. Results are saved under `results/<repo-name>/`. CLI exit codes: 0 for passing execution, 1 for unsuccessful repair, 2 for setup errors. `execution_only` means that no tests were found.

## Model-backed operation

Add your key to the local `.env` file:

```text
ASU_AIR_API_KEY=your_key_here
```

The program loads `.env` by itself. The file is ignored by Git and must not be committed. Save a broken reposiotry under `tests/broken_repos/`, and while excuting the erapir, give only its folder name:

```console
uv run repo-repair-agent broken-repo-folder-name
```

The default limit is five repair rounds with 30 seconds for each command. Change these only when needed:

```console
uv run repo-repair-agent broken-repo-folder-name --rounds 8 --timeout 60
```

You can still provide a full path when the repository is saved somewhere else.

Every run saves a patch and report. After a successful repair, it also saves only the changed files:

```text
results/
└── repo-name/
    ├── fixed_files/
    ├── repair.patch
    ├── report.json
    └── steps_to_run.txt
```

Add `--agent-logs` when you need to debug agent behavior:

```console
uv run repo-repair-agent broken-repo-folder-name --agent-logs
```

This saves `agent_logs.json` in the repository's result folder. It records each
Fixer and Judge prompt, response, purpose, and timing. API keys and authorization
headers are never added. This file may contain repository source text, so Git
ignores it by default. Without `--agent-logs`, prompts and responses are not
collected or saved.

Run the repair-agent tests with:

```console
uv run python -m unittest discover -s tests -v
```

### Configure agent models

Edit `config.json` to select the Fixer and Judge models:

```json
{
  "endpoint": "https://openai.rc.asu.edu/v1",
  "fixer_model": "qwen3-coder-30b-a3b-instruct",
  "judge_model": "qwen3-235b-a22b",
  "api_timeout_seconds": 300
}
```

`endpoint` is an HTTPS API base URL (such as `https://openai.rc.asu.edu/v1`) or a complete chat-completions URL, shared by both agents. The client appends `/chat/completions` to base URLs. The API key is always read from `ASU_AIR_API_KEY` in `.env`; never put the key in this file. Model names must be available through the selected endpoint. Use different Fixer and Judge models for independent review. The Main Orchestrator is not yet model-driven and has no model setting.

Use a separate configuration with:

```console
uv run repo-repair-agent "<repo-path>" --config "<config-path>"
```

By default, the runner loads `config.json` from the project root, regardless of the current working directory. The endpoint and two model fields are required. `api_timeout_seconds` is optional and defaults to 300 (integer, 1–3600). It controls socket waits, including the wait for initial response headers; it is not a total wall-clock deadline for an active stream. The CLI `--timeout` separately limits each target execution or installation command. Unknown fields are rejected. Offline `--script` mode does not load model configuration or require a key.

The configuration selects ASU Voyager. Requests enable SSE streaming with usage reporting. Progress goes to stderr every 15 seconds while waiting and when reasoning or answer generation begins; model reasoning and answer text are not printed. The run report records per-request timing, request IDs, finish reasons, and token usage when provided. A server that returns a regular JSON completion instead is also accepted.

Answers must contain a JSON object, either plain or wrapped in a single Markdown code fence. Prose surrounding JSON, malformed JSON, truncated replies, and streams missing `[DONE]` stop the run without applying that response. Code edits require Judge approval and may change multiple existing Python source files. Test edits, path traversal, and repeated actions are rejected. Regressions in previously passing execution or tests trigger rollback.

Each local Python repair run creates a clean `.venv` inside the selected broken repository. Git ignores this folder, so it is not pushed. Docker repairs create `.venv` only inside their temporary container work copy. Declared `requirements.txt` dependencies are installed automatically. Extra plain package names proposed by the Fixer are downloaded only after Judge approval. After a successful installation, the package is added to a repaired `requirements.txt` and saved under `fixed_files`. Use `--no-install` when dependency downloads must be disabled. Target dependencies remain isolated from the repair agent. Every result folder includes `steps_to_run.txt` with instructions for copying repaired files first, using `.venv`, installing dependencies, running the application, and running detected tests.

For Python repositories, `.python-version` or `requires-python` in `pyproject.toml` selects the interpreter. In automatic backend mode, the agent first looks for that version locally without downloading Python. If it is unavailable, the agent falls back to a matching Python Docker image. A missing image is downloaded only when `--allow-docker-pull` is provided. Explicit `--backend local` never falls back to Docker.

## Docker execution

The agent can run a trusted repair target in a resource-limited Docker
container. The original repository is copied first; only that temporary copy is
mounted at `/workspace`. A repository with a `Dockerfile`, or any non-Python
repository, selects Docker in `--backend auto` mode; Docker can also be
selected explicitly:

```console
uv run repo-repair-agent my-repository --backend docker
```

Repository markers select the toolchain and a default image: `Cargo.toml`
(cargo, `rust:bookworm`), `go.mod` (go, `golang:bookworm`), `meson.build`,
`SConstruct`, `CMakeLists.txt`, `configure`/`Makefile` (all `ubuntu:24.04`),
and Python files (`python:3.12-slim`). Use `--docker-image` to override it. The agent checks that
Docker is installed and its engine is running, then checks for the selected
image locally. It never downloads an image silently. When the report says that
an image is missing, review the image name and re-run with
`--allow-docker-pull` to explicitly approve the download:

```console
uv run repo-repair-agent my-repository --backend docker --allow-docker-pull
```

Containers have no network for application and test commands. Network is
enabled only while installing dependencies that the caller or Judge has already
allowed. The container runs with CPU, memory, PID, capability, filesystem, and
temporary-directory limits.

For a non-Python repository the "application" step is the build command
(`cargo build`, `go build ./...`, `cmake`, `meson`, `scons`, or
`./configure && make`) and the test step is the matching test command
(`cargo test`, `go test ./...`, `ctest`, `meson test`, `make check`) when test
files or test targets are found. Dependencies are fetched once with network
(`cargo fetch`, `go mod download`); the Fixer's `install` action maps to
`cargo add` or `go get`. The stock `ubuntu:24.04` image has no compiler, so
C/C++ repositories need `--docker-image` pointing at an image with their build
tools and system libraries. Compiled builds need a long `--timeout` (minutes,
not the 30 s default).

## Current boundaries

This is the first implementation slice, not the complete milestone system. It supports root `main.py`, `app.py`, or `run.py` and standard-library unittest discovery (`test*.py`, under `tests/` when present). Pytest-only projects are not supported. Non-Python repairs run only in Docker. Detected tests are not a guarantee of adequate functional coverage.

A temporary directory and virtual environment are **not a security sandbox**. Target programs and package installers run with your user permissions; use only trusted fixtures. Relevant source files and execution output are sent to Voyager in live mode. Do not use repositories containing secrets or private data. Reports contain source edits and command output; review before sharing.

Next milestones: Docker isolation and image selection; model-driven orchestration and context selection; broader project detection; validated GitHub PR publishing; benchmark tasks, hidden functional checks, labeled Judge patches, and comparisons against the two-agent and single-call baselines. The current loop is deterministic orchestration and has no Main Orchestrator model. No PRs are created or pushed.

## Layout and contribution

- `src/`: production code containing the CLI, agents, repository inspection, execution, validation, and repair loop.
- `.env`: local API key file; ignored by Git.
- `config.json`: model names and the shared API endpoint.
- `pyproject.toml`, `requirements.txt`, `uv.lock`, `.python-version`: project metadata, dependency declarations, lock file, and development Python version.
- `tests/broken_repos/`: deliberately broken sample repositories and offline repair scripts.
- `tests/`: unit and integration checks for the repair agent.

Use four-space Python indentation, descriptive snake_case names, and standard-library unittest. Run the test command above before submitting changes. Describe behavior changes, validation, and scope limitations in pull requests. Never commit API keys, local environments, or private run logs.
