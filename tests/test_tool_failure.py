"""Why this exists:
Tests tool execution timeouts and unhandled failure wrapping via run_tool.
Ensures slow operations raise ToolTimeout and raw server exceptions are masked as ToolFailure.
"""
import time
import pytest

from app.tools import (
    NotFound,
    RuleViolation,
    ToolFailure,
    ToolTimeout,
    run_tool,
)


def test_run_tool_timeout():
    """A tool function exceeding the timeout limit is terminated with ToolTimeout."""
    def slow_tool():
        time.sleep(0.5)
        return "finished"

    # Set threshold to 0.1s to test timeout fast
    with pytest.raises(ToolTimeout) as exc_info:
        run_tool(slow_tool, timeout=0.1)

    assert "Tool execution timed out" in str(exc_info.value)


def test_run_tool_masks_unexpected_exception():
    """Unhandled exceptions are converted to ToolFailure with a safe, plain error message."""
    def buggy_tool():
        raise ZeroDivisionError("division by zero internal stack trace details")

    with pytest.raises(ToolFailure) as exc_info:
        run_tool(buggy_tool, timeout=1.0)

    # Must NOT leak raw internal stack trace or exception text
    assert "division by zero" not in str(exc_info.value)
    assert "An unexpected failure occurred while processing your request." in str(exc_info.value)


def test_run_tool_passes_through_domain_exceptions():
    """Domain-typed exceptions (NotFound, RuleViolation) pass through without being masked."""
    def not_found_tool():
        raise NotFound("Roster not found")

    with pytest.raises(NotFound) as exc_info:
        run_tool(not_found_tool)
    assert "Roster not found" in str(exc_info.value)

    def rule_violation_tool():
        raise RuleViolation("Cutoff window passed")

    with pytest.raises(RuleViolation) as exc_info:
        run_tool(rule_violation_tool)
    assert "Cutoff window passed" in str(exc_info.value)
