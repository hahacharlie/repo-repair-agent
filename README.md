# Repository Repair Agent

An initial Python implementation of the repository-repair capstone design. It finds a Python start file, copies the repository into a temporary folder, creates a clean Python environment, runs the app and its unittest tests, and works through Fixer proposals and Judge reviews. The original repository stays unchanged.

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
    └── report.json
```

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

By default, the runner loads `config.json` from the project root, regardless of the current working directory. The four endpoint/model/key fields are required. `api_timeout_seconds` is optional and defaults to 300 (integer, 1–3600). It controls socket waits, including the wait for initial response headers; it is not a total wall-clock deadline for an active stream. The CLI `--timeout` separately limits each target execution or installation command. Unknown fields are rejected. Offline `--script` mode does not load model configuration or require a key.

The configuration selects ASU Voyager. Requests enable SSE streaming with usage reporting. Progress goes to stderr every 15 seconds while waiting and when reasoning or answer generation begins; model reasoning and answer text are not printed. The run report records per-request timing, request IDs, finish reasons, and token usage when provided. A server that returns a regular JSON completion instead is also accepted.

Answers must contain a JSON object, either plain or wrapped in a single Markdown code fence. Prose surrounding JSON, malformed JSON, truncated replies, and streams missing `[DONE]` stop the run without applying that response. Code edits require Judge approval and may change multiple existing Python source files. Test edits, path traversal, and repeated actions are rejected. Regressions in previously passing execution or tests trigger rollback.

Temporary target environments are created with `uv venv` using the runner's Python interpreter. Dependency installation is disabled by default. `--allow-install` enables declared `requirements.txt` installation and Fixer-requested plain package names through `uv pip install --python <target-python>` inside the temporary environment. Installation does not require Judge approval. Use `uv add <package>` for runner dependencies; target dependencies remain isolated from the runner.

## Current boundaries

This is the first implementation slice, not the complete milestone system. It supports root `main.py`, `app.py`, or `run.py` and standard-library unittest discovery (`test*.py`, under `tests/` when present). Pytest-only projects and other languages are not supported. Detected tests are not a guarantee of adequate functional coverage.

A temporary directory and virtual environment are **not a security sandbox**. Target programs and package installers run with your user permissions; use only trusted fixtures. Relevant source files and execution output are sent to Voyager in live mode. Do not use repositories containing secrets or private data. Reports contain source edits and command output; review before sharing.

Next milestones: Docker isolation and image selection; model-driven orchestration and context selection; broader project detection; validated GitHub PR publishing; benchmark tasks, hidden functional checks, labeled Judge patches, and comparisons against the two-agent and single-call baselines. The current loop is deterministic orchestration and has no Main Orchestrator model. No PRs are created or pushed.

## Layout and contribution

- `src/`: production code containing the CLI, agents, repository inspection, execution, validation, and repair loop.
- `.env`: local API key file; ignored by Git.
- `config.json`: model names, shared API endpoint, and credential environment-variable name.
- `pyproject.toml`, `requirements.txt`, `uv.lock`, `.python-version`: project metadata, dependency declarations, lock file, and development Python version.
- `tests/broken_repos/`: deliberately broken sample repositories and offline repair scripts.
- `tests/`: unit and integration checks for the repair agent.

Use four-space Python indentation, descriptive snake_case names, and standard-library unittest. Run the test command above before submitting changes. Describe behavior changes, validation, and scope limitations in pull requests. Never commit API keys, local environments, or private run logs.
