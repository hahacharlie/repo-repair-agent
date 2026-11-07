"""Baseline agents used for fair repair comparisons."""

from pathlib import PurePosixPath
import re
import shlex

from agents import ModelAgents, ModelResponseError, basic_run_steps
from constants import (
    STAKEHOLDER_ENTRY_CHARS,
    STAKEHOLDER_FIXER_MODEL,
    STAKEHOLDER_FIXER_PROMPT,
    STAKEHOLDER_JUDGE_MODEL,
    STAKEHOLDER_JUDGE_PROMPT,
    STAKEHOLDER_PURPOSE_CHARS,
    STAKEHOLDER_SOURCE,
    STAKEHOLDER_SOURCE_COMMIT,
)


def _purpose(files, repository):
    for name in ("README.md", "readme.md", "README.txt", "README.rst"):
        if name in files:
            return files[name][:STAKEHOLDER_PURPOSE_CHARS].strip()
    entry = repository.get("entry")
    if entry in files:
        return ("Entry point (" + entry + "):\n"
                + files[entry][:STAKEHOLDER_ENTRY_CHARS].strip())
    return "A software project."


def _tree(files):
    """Build the same simple file tree used by the stakeholder prompt."""
    root = {}
    for name in sorted(files):
        node = root
        for part in PurePosixPath(name).parts:
            node = node.setdefault(part, {})

    lines = ["repo/"]

    def add(node, level):
        for name, children in sorted(node.items()):
            suffix = "/" if children else ""
            lines.append("  " * level + name + suffix)
            add(children, level + 1)

    add(root, 1)
    return "\n".join(lines)


def _file_contents(files):
    return "\n\n".join(
        "=== " + name + " ===\n" + text
        for name, text in sorted(files.items())
    )


def _error_output(observation):
    output = []
    for name in ("app", "tests"):
        result = observation.get(name)
        if not isinstance(result, dict):
            continue
        if result.get("exit_code") == 0 and not result.get("timeout"):
            continue
        text = (result.get("stdout", "") + result.get("stderr", "")).strip()
        output.append(text or name + " failed")
    return "\n".join(output)


def _attempt_log(history):
    if not history:
        return "(none)"
    lines = []
    for number, record in enumerate(history, start=1):
        action = record.get("action", {})
        kind = action.get("action", "?")
        if kind == "install":
            installation = record.get("installation", {})
            result = (
                "ok" if installation.get("exit_code") == 0 else "failed")
            lines.append(
                f"Round {number}: COMMAND\n"
                f"  cmd     : install {action.get('package', '')}\n"
                f"  result  : {result}"
            )
        elif kind == "edit":
            review = record.get("judge", {})
            verdict = "APPROVE" if review.get("approved") else "REJECT"
            header = f"Round {number}: EDIT"
            if verdict == "REJECT":
                header += " [REJECTED — DO NOT PROPOSE THIS AGAIN]"
            names = ", ".join(action.get("files", {}))
            lines.append(
                header + "\n"
                f"  file    : {names}\n"
                f"  verdict : {verdict}\n"
                f"  reason  : {review.get('reason', '')}"
            )
        else:
            lines.append(
                f"Round {number}: REPORT\n"
                f"  detail  : {action.get('reason', '')}"
            )
    return "\n\n".join(lines)


def parse_stakeholder_fixer(raw):
    """Convert the stakeholder text format into one safe repair action."""
    text = raw.strip()
    text = re.sub(r"^```[^\n]*\n?", "", text, count=1)
    text = re.sub(r"\n?```\s*$", "", text, count=1).strip()
    found = re.match(r"ACTION:\s*(COMMAND|EDIT|REPORT)", text, re.I)
    if not found:
        raise ModelResponseError(
            "Stakeholder Fixer response could not be parsed")
    action = found.group(1).upper()
    if action == "COMMAND":
        detail = re.search(r"DETAIL:\s*(.+)", text, re.I)
        if not detail:
            raise ModelResponseError("Stakeholder COMMAND has no detail")
        command = re.sub(r"\s+#.*$", "", detail.group(1)).strip()
        try:
            parts = shlex.split(command)
        except ValueError:
            parts = []
        prefixes = (
            ["pip", "install"],
            ["pip3", "install"],
            ["python", "-m", "pip", "install"],
            ["python3", "-m", "pip", "install"],
            ["cargo", "add"],
            ["go", "get"],
        )
        package = next(
            (parts[len(prefix)] for prefix in prefixes
             if parts[:len(prefix)] == prefix and len(parts) == len(prefix) + 1),
            None,
        )
        if package is None:
            raise ModelResponseError(
                "Stakeholder COMMAND is not a supported package install")
        return {
            "action": "install",
            "package": package,
            "reason": "Stakeholder Fixer command: " + command,
        }
    if action == "EDIT":
        content_header = re.search(r"\nCONTENT:\s*\n", text, re.I)
        header = text[:content_header.start()] if content_header else text
        filename = re.search(r"FILE:\s*(.+)", header, re.I)
        reason = re.search(r"JUSTIFICATION:\s*(.+)", header, re.I)
        if not filename or not content_header:
            raise ModelResponseError(
                "Stakeholder EDIT has no file or content")
        return {
            "action": "edit",
            "files": {
                filename.group(1).strip(): (
                    text[content_header.end():].rstrip() + "\n")
            },
            "reason": reason.group(1).strip() if reason else "Fix the error",
        }
    detail = re.search(r"DETAIL:\s*(.+)", text, re.I)
    return {
        "action": "give_up",
        "reason": detail.group(1).strip() if detail else "Needs a human",
    }


def parse_stakeholder_judge(raw):
    verdict = re.match(r"VERDICT:\s*(APPROVE|REJECT)", raw.strip(), re.I)
    reason = re.search(r"REASON:\s*([^\n]+)", raw, re.I)
    if not verdict:
        raise ModelResponseError(
            "Stakeholder Judge response could not be parsed")
    return {
        "approved": verdict.group(1).upper() == "APPROVE",
        "reason": reason.group(1).strip() if reason else "",
    }


class StakeholderAgents(ModelAgents):
    """Run the stakeholder's Fixer and edit-only Judge baseline."""

    baseline_name = "stakeholder"
    review_policy = "edits_only"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.project_purpose = "A software project."

    def propose(self, context):
        files = context["files"]
        self.project_purpose = _purpose(files, context["repository"])
        user = (
            "Project: " + self.project_purpose
            + "\n\nFiles:\n" + _tree(files)
            + "\n\nFile contents:\n" + _file_contents(files)
            + "\n\nError:\n" + _error_output(context["observation"])
            + "\n\nAlready tried:\n" + _attempt_log(context["history"])
            + "\n\nWhat's your next action?"
        )
        raw = self.request_text(
            STAKEHOLDER_FIXER_MODEL,
            STAKEHOLDER_FIXER_PROMPT,
            user,
            agent="fixer",
            purpose="stakeholder_repair_proposal",
        )
        return parse_stakeholder_fixer(raw)

    def review(self, context):
        filename, current = next(iter(context["before"].items()))
        proposed = context["after"][filename]
        user = (
            "Project: " + self.project_purpose
            + "\n\nFile: " + filename
            + "\n\nCurrent:\n" + current
            + "\n\nProposed:\n" + proposed
            + "\n\nReason given: " + context["reason"]
            + "\n\nDoes this edit remove anything the project needs?"
        )
        raw = self.request_text(
            STAKEHOLDER_JUDGE_MODEL,
            STAKEHOLDER_JUDGE_PROMPT,
            user,
            agent="judge",
            purpose="stakeholder_code_review",
        )
        return parse_stakeholder_judge(raw)

    def generate_run_steps(self, context):
        steps = basic_run_steps(context)
        return steps.replace(
            "Approved packages recorded in requirements.txt: ",
            "Install approved packages: python -m pip install ",
        )


class FixerOnlyAgents(ModelAgents):
    """Run the proposed Fixer without an edit Judge."""

    baseline_name = "fixer-only"
