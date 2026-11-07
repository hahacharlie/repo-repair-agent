# Repository Repair Agent

This tool tries to fix a repository that does not run. Repair changes are made
on a copy, so your original source files are not changed.

## What you need

- Python 3.10 or newer
- `pip`
- An ASU Voyager API key for AI repairs
- Docker for non-Python repositories or when the required Python version is
  not installed on your machine

Docker must be installed and running before it can be used. The offline sample
does not need an API key or Docker.

## Set up the project

Open a terminal in this project folder and run:

```console
python -m pip install -r requirements.txt
```

These commands work on Windows, macOS, and Linux. If your system uses
`python3`, use it instead of `python` in the setup command.

## Add your API key

Get a key from the [ASU Voyager portal](https://voyager.rc.asu.edu):

1. Connect to the ASU network or ASU VPN.
2. Sign in with your ASURITE account.
3. Open your profile and select **Keys**.
4. Create and copy an API key.

The first AI repair creates a `.env` file in this project folder. Open it and
add your key:

```text
ASU_AIR_API_KEY=your_key_here
```

Save the file, then run the repair command again. Git ignores `.env`, so the
key is not pushed. Do not share this file.

## Add a repository

Put the repository inside:

```text
input/broken_repos/
```

For example:

```text
input/broken_repos/my-repository/
```

You only need to give the folder name when running the agent.

## Run a repair

```console
uv run repo-repair-agent my-repository
```

To try the offline sample:

```console
uv run repo-repair-agent undefined_greeting_variable
```

The tool will:

1. Make a temporary copy of the repository.
2. Find the language, run command, and tests.
3. Run the repository and collect the error.
4. Ask the Fixer for one repair.
5. Ask the Judge to review code changes.
6. Run the repository again to check the repair.

Package installs do not need Judge approval. Code changes do.

## Results

Results are saved here:

```text
results/<repo-name>/
```

The folder may contain:

- `report.json`: repair result and metrics
- `fixed_files/`: files changed by a successful repair
- `steps_to_run.txt`: simple steps for running the repaired repository
- `agent_logs.json`: optional debugging log

The original repository stays unchanged.

### Save agent logs

Use `--agent-logs` when you want to see the repair progress, prompts, and
responses:

```console
uv run repo-repair-agent my-repository --agent-logs
```

The logging option creates one file:

```text
results/my-repository/agent_logs.json
```

The file is updated during the run. If the repair stops early, the completed
log entries are still available.

## Evaluation modes

Run the proposed system:

```console
uv run repo-repair-agent my-repository
```

Run the stakeholder Fixer-Judge baseline:

```console
uv run repo-repair-agent my-repository --baseline stakeholder
```

Run the Fixer-only baseline:

```console
uv run repo-repair-agent my-repository --baseline fixer-only
```

The stakeholder baseline uses these models:

- Fixer: `qwen3-coder-30b-a3b-instruct`
- Judge: `qwen3-235b-a22b-instruct-2507`

It comes from
[sakethreddy26/capstone-proposal](https://github.com/sakethreddy26/capstone-proposal),
commit
[2b0cade1c6f7c3445e637ee83e7c247c083f5f3f](https://github.com/sakethreddy26/capstone-proposal/commit/2b0cade1c6f7c3445e637ee83e7c247c083f5f3f).

Each run replaces the previous results for that repository. Use different
output folders when you want to keep every evaluation run:

```console
uv run repo-repair-agent my-repository --output results/proposed
uv run repo-repair-agent my-repository --baseline stakeholder --output results/stakeholder
uv run repo-repair-agent my-repository --baseline fixer-only --output results/fixer-only
```

## Useful options

- `--rounds 8`: allow up to eight repair attempts. The default is five.
- `--timeout 300`: allow each repository command to run for 300 seconds. The
  default is 120 seconds.
- `--agent-timeout 1800`: allow 30 minutes in total for Fixer and Judge API
  calls. The default is 20 minutes.
- `--no-install`: do not download repository dependencies.
- `--backend local`: use only the local machine.
- `--backend docker`: use Docker.
- `--docker-image IMAGE`: choose a Docker image.
- `--allow-docker-pull`: allow the selected Docker image to be downloaded.
- `--config FILE`: use another model configuration file.
- `--output FOLDER`: choose where results are saved.

Run this command to see every option:

```console
uv run repo-repair-agent --help
```

## Python repositories

- A `.venv` is created inside the selected broken repository.
- Git ignores `.venv`.
- Packages in `requirements.txt` are installed automatically.
- If the Fixer installs a new package, the repaired `requirements.txt` is
  saved in `fixed_files`.
- `.python-version` or `requires-python` in `pyproject.toml` can request a
  Python version.
- If that version is not installed, automatic mode can use Docker.
- Docker images are downloaded only when `--allow-docker-pull` is used.

## Docker

Docker is used for non-Python repositories. It can also be used when a Python
repository needs a Python version that is not installed locally.

To request Docker directly:

```console
uv run repo-repair-agent my-repository --backend docker
```

If the image is missing, the tool stops and shows the image name. After
checking the image, allow the download with:

```console
uv run repo-repair-agent my-repository --backend docker --allow-docker-pull
```

The repository runs inside a temporary container copy. App and test commands
do not have network access. Network access is used only when dependencies need
to be downloaded.

C and C++ repositories may need a custom image containing their compiler and
system libraries:

```console
uv run repo-repair-agent my-repository --backend docker --docker-image IMAGE
```

## Metrics

The console and `report.json` show:

- correct repair rate
- Fixer tokens
- Judge tokens
- total tokens
- total run time

A correct repair means the repository failed first and passed its required
checks after the repair. A repository that already worked is not counted as a
repair.

Token counts are local estimates. They do not download a model tokenizer.

## Change the models

Model settings are in `config.json`:

```json
{
  "endpoint": "https://openai.rc.asu.edu/v1",
  "fixer_model": "qwen3-coder-30b-a3b-instruct",
  "judge_model": "qwen3-235b-a22b",
  "api_timeout_seconds": 300
}
```

- `endpoint`: model API address
- `fixer_model`: model that suggests repairs
- `judge_model`: model that reviews code changes
- `api_timeout_seconds`: wait time for one API request

Keep the API key in `.env`, not in `config.json`.

If a model returns invalid JSON, the tool tries two more times before stopping.

## Current limits

- Python entry files must be named `main.py`, `app.py`, or `run.py`.
- Python tests use standard `unittest` discovery.
- Pytest-only projects are not yet supported.
- Non-Python repositories run in Docker.
- The tool does not create GitHub pull requests yet.
- The current proposed pipeline has a Fixer and Judge. A model-based Main
  Orchestrator is planned.

Use only repositories you trust. Repository commands and package installers
run code on your machine or in Docker. During AI repairs, selected source files
and error messages are sent to ASU Voyager. Do not use a repository containing
passwords, API keys, or private data.

## Project folders

- `src/`: repair-agent code
- `input/broken_repos/`: repositories given to the agent
- `results/`: repair results
- `tests/`: repair-agent tests
- `.env`: local API key
- `config.json`: model settings

## Run the tests

```console
uv run python -m unittest discover -s tests -v
```
