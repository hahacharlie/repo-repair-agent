# Repository Repair Agent

An implementation of the repository-repair capstone design for Python and selected compiled-language repositories. It copies the repository into a temporary folder, runs the detected application or build and tests, and works through Fixer proposals and independent Judge reviews. Repairs are restricted to the lines required to resolve the observed blocker; unrelated cleanup and cosmetic edits are rejected or removed. The original source files stay unchanged.

## Requirements and quick start

Minimum requirements:

- Python 3.10 or newer with `pip`. The CLI checks this at startup and stops
  before setup when the installed Python is too old.
- Docker Desktop on Windows/macOS or Docker Engine on Linux, with Docker
  running. Docker is required for non-Python repositories and as a fallback
  when the required Python version is not installed locally.
- An ASU AI API key for model-backed repairs. Offline scripted repairs do   not need an API key.

The commands below use the same form on Windows, macOS, and Linux. If your
system uses `python3` instead of `python`, use `python3` in the setup command.

Install the project runner one time from the project folder:

```console
python -m pip install -r requirements.txt
```

Run the offline sample without an API key:

```console
uv run repo-repair-agent undefined_greeting_variable
```

The offline example repairs an undefined variable and checks greeting behavior. Its scripted approval is a fixed test run, not proof that a model Judge works well. Results are saved under `results/<repo-name>/`. CLI exit codes: 0 for passing execution, 1 for unsuccessful repair, 2 for setup errors. `execution_only` means that no tests were found. If the initial application/build and detected tests already pass, the CLI exits with code 0, prints `No errors found. No repair was needed.`, and does not call either agent.

## Model-backed operation

On the first model-backed run, the program creates `.env` in the project root
when it does not exist and prints its location. Add your key like this:

```text
ASU_AIR_API_KEY=your_key_here
```

Run the same command again after saving the key. The program loads `.env` by
itself. The file is ignored by Git and must not be committed. Offline `--script`
runs do not need or create `.env`. Save a broken repository under
`input/broken_repos/`, then give only its folder name when running a repair:

```console
uv run repo-repair-agent broken-repo-folder-name
```

The default limit is five repair rounds with 30 seconds for each command. Change these only when needed:

```console
uv run repo-repair-agent broken-repo-folder-name --rounds 8 --timeout 60
```

You can still provide a full path when the repository is saved somewhere else.

Every run saves `report.json`. A successful repair saves the changed file
contents and run instructions. Clean runs and failures with no source changes
do not create fixed-file artifacts:

```text
results/
└── repo-name/
    ├── fixed_files/       # successful repairs with changed files only
    ├── report.json
    └── steps_to_run.txt   # only after a repair attempt needs run guidance
```

Add `--agent-logs` when you need to debug agent behavior:

```console
uv run repo-repair-agent broken-repo-folder-name --agent-logs
```

This saves `agent_logs.json` in the repository's result folder. It records each
Fixer and Judge prompt, response, purpose, and timing. API keys and authorization
headers are never added. This file may contain repository source text, so Git
ignores it by default. Without `--agent-logs`, prompts and responses are not
collected or saved. A clean no-op run makes no agent requests and does not save
an empty agent log. Live request messages identify both roles, for example
`[fixer: model-name]` and `[judge: model-name]`.

The CLI prints concise progress lines before potentially slow setup, dependency,
build, and test phases so a quiet command does not appear stuck.

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

Configuration fields:

- `endpoint`: HTTPS API base URL or complete chat-completions URL. The client
  adds `/chat/completions` when needed.
- `fixer_model`: model that proposes repairs and run instructions.
- `judge_model`: separate model that reviews edits and dependency installs.
- `api_timeout_seconds`: optional API wait limit from 1 to 3600 seconds. The
  default is 300.
- The model names must be available through the selected endpoint.
- Keep the API key in `.env`, never in `config.json`.

Use a separate configuration with:

```console
uv run repo-repair-agent "<repo-path>" --config "<config-path>"
```

Other model behavior:

- The project-root `config.json` is used by default. `--config` selects another
  file.
- Offline `--script` mode does not load model configuration or require a key.
- Model replies must contain one JSON object. Invalid or incomplete replies are
  rejected without applying them.
- Code edits, dependency installs, and Fixer requests to give up require Judge
  review.
- The Fixer receives the engine's dependency-installation capabilities and must
  not propose unsupported installs as source-code fixes.
- The Fixer must change only lines required by the observed failure. The Judge
  rejects unrelated cleanup, formatting, blank-line, comment, import, and
  latent-issue changes. The engine also preserves separable cosmetic-only
  regions before review.
- Test edits, unsafe paths, repeated actions, and regressions are rejected.
- Request timing and token usage are added to the run report when available.
- `--timeout` controls target commands; `api_timeout_seconds` controls model API
  waits.

### Python environments and dependencies

- Local Python repairs create `.venv` inside the selected broken repository.
- Git ignores `.venv`, so it is not pushed.
- Docker repairs create `.venv` only inside the temporary container copy.
- Dependencies from `requirements.txt` are installed automatically.
- New packages require Judge approval before download.
- Approved packages are added to the repaired `requirements.txt` and saved in
  `fixed_files`.
- `--no-install` disables dependency downloads.
- After a repair attempt, `steps_to_run.txt` explains how to apply and run the
  repaired files. It is not generated for an already-clean repository.

### Python version selection

- `.python-version` or `requires-python` in `pyproject.toml` selects Python.
- Automatic mode first looks for that Python version locally without
  downloading it.
- If the version is unavailable locally, automatic mode uses a matching Docker
  image.
- A missing image is downloaded only with `--allow-docker-pull`.
- `--backend local` never falls back to Docker.

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

Possible next milestones include broader project detection, validated GitHub PR publishing, benchmark tasks, hidden functional checks, labeled Judge patches, and comparisons against two-agent and single-call baselines. The current loop uses deterministic orchestration around separate Fixer and Judge models; it has no Main Orchestrator model. No PRs are created or pushed.

## Layout and contribution

- `src/`: production code containing the CLI, agents, repository inspection, execution, validation, and repair loop.
- `.env`: local API key file; ignored by Git.
- `config.json`: model names and the shared API endpoint.
- `pyproject.toml`, `requirements.txt`, `uv.lock`, `.python-version`: project metadata, dependency declarations, lock file, and development Python version.
- `input/broken_repos/`: broken repositories and offline repair scripts used as repair inputs.
- `tests/`: unit and integration checks for the repair agent.

Use four-space Python indentation, descriptive snake_case names, and standard-library unittest. Run the test command above before submitting changes. Describe behavior changes, validation, and scope limitations in pull requests. Never commit API keys, local environments, or private run logs.
