"""Measure whether repository repairs are correct."""


def command_passed(result):
    """Return whether one saved command passed."""
    return (
        isinstance(result, dict)
        and result.get("exit_code") == 0
        and not result.get("timeout", False)
    )


def observation_passed(observation):
    """Return whether every command in one check passed."""
    if not isinstance(observation, dict):
        return False
    commands = [value for value in observation.values() if value is not None]
    return bool(commands) and all(command_passed(value) for value in commands)


def environment_repair_passed(report):
    """Return whether an approved install fixed the failing command."""
    final = report.get("final", {})
    if not command_passed(final.get("app")):
        return False
    for record in report.get("history", []):
        action = record.get("action", {})
        judge = record.get("judge", {})
        observation = record.get("observation", {})
        if (
            action.get("action") == "install"
            and judge.get("approved") is True
            and command_passed(record.get("installation"))
            and command_passed(observation.get("app"))
        ):
            return True
    return False


def evaluate_correct_repair(report):
    """Check whether one run fixed a real failure and passed its tests."""
    initial = report.get("initial")
    final = report.get("final")

    if not isinstance(initial, dict):
        return {
            "evaluated": False,
            "correct": False,
            "reason": "The original repository failure was not observed",
        }
    if observation_passed(initial):
        return {
            "evaluated": False,
            "correct": False,
            "reason": "The repository did not fail before the repair",
        }
    if not isinstance(final, dict):
        return {
            "evaluated": True,
            "correct": False,
            "reason": "No final application and test results were recorded",
        }
    if final.get("tests") is None and environment_repair_passed(report):
        return {
            "evaluated": True,
            "correct": True,
            "reason": "The environment issue was fixed and the command passed",
        }
    if final.get("tests") is None:
        return {
            "evaluated": True,
            "correct": False,
            "reason": "No functional tests were found",
        }
    if report.get("status") != "passed" or not observation_passed(final):
        return {
            "evaluated": True,
            "correct": False,
            "reason": "The application or its tests still fail",
        }
    return {
        "evaluated": True,
        "correct": True,
        "reason": "The original failure was fixed and all tests passed",
    }


def summarize_repair_rate(reports):
    """Combine verified repair outcomes from several run reports."""
    evaluated_runs = 0
    correct_repairs = 0
    failed_reasons = set()
    for report in reports:
        outcome = report.get("metrics", {}).get("correct_repair")
        if not isinstance(outcome, dict):
            outcome = evaluate_correct_repair(report)
        if outcome.get("evaluated") is True:
            evaluated_runs += 1
            if outcome.get("correct") is True:
                correct_repairs += 1
            elif isinstance(outcome.get("reason"), str):
                failed_reasons.add(outcome["reason"])

    percentage = (
        round(correct_repairs * 100 / evaluated_runs, 2)
        if evaluated_runs else None
    )
    summary = {
        "correct_repairs": correct_repairs,
        "evaluated_runs": evaluated_runs,
        "percentage": percentage,
    }
    if failed_reasons == {"No functional tests were found"}:
        summary["note"] = "no_tests_found"
    return summary
