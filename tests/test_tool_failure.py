"""Why this exists:
Tests LLM parsing failure modes, retries, validation errors, timeouts, and budget limits.
Uses FakeLLM to simulate provider failures deterministically without real API calls.
"""
import pytest

from app.llm import (
    FakeLLM,
    LLMBudget,
    LLMBudgetExceeded,
    LLMParseError,
    LLMTimeout,
    LLMUnavailable,
    parse_message,
)


def test_retry_on_initial_garbage_response():
    """Garbage output on first attempt triggers a single retry with error hint and succeeds."""
    responses = [
        "not a valid json output",
        '{"intent": "change_time", "new_time": "19:00", "trip_type": "drop", "date": "today"}',
    ]
    fake_llm = FakeLLM(responses=responses)
    budget = LLMBudget()

    parsed = parse_message(fake_llm, budget, "move my drop to 7pm")
    assert parsed.intent == "change_time"
    assert parsed.new_time == "19:00"
    assert budget.calls == 2
    assert len(fake_llm.prompts) == 2
    assert "Notice: Your previous response was invalid" in fake_llm.prompts[1]


def test_double_garbage_raises_parse_error():
    """Two consecutive malformed responses exhaust retry and raise LLMParseError."""
    fake_llm = FakeLLM(responses=["garbage 1", "garbage 2"])
    budget = LLMBudget()

    with pytest.raises(LLMParseError):
        parse_message(fake_llm, budget, "hello")
    assert budget.calls == 2


def test_unknown_intent_retried():
    """Unrecognized intent value fails schema validation and triggers retry."""
    responses = [
        '{"intent": "hack_the_system", "new_time": "19:00", "trip_type": "drop", "date": "today"}',
        '{"intent": "other", "new_time": null, "trip_type": null, "date": null}',
    ]
    fake_llm = FakeLLM(responses=responses)
    budget = LLMBudget()

    parsed = parse_message(fake_llm, budget, "hello")
    assert parsed.intent == "other"
    assert budget.calls == 2


def test_invalid_time_format_retried():
    """Times with bad format (such as '25:99' or '8pm') fail validation and trigger retry."""
    responses = [
        '{"intent": "change_time", "new_time": "25:99", "trip_type": "drop", "date": "today"}',
        '{"intent": "change_time", "new_time": "20:00", "trip_type": "drop", "date": "today"}',
    ]
    fake_llm = FakeLLM(responses=responses)
    budget = LLMBudget()

    parsed = parse_message(fake_llm, budget, "shift to 8")
    assert parsed.intent == "change_time"
    assert parsed.new_time == "20:00"
    assert budget.calls == 2


def test_llm_timeout_raised():
    """Simulated timeout exception from LLM client raises LLMTimeout."""
    fake_llm = FakeLLM(responses=[LLMTimeout("Connection timed out")])
    budget = LLMBudget()

    with pytest.raises(LLMTimeout):
        parse_message(fake_llm, budget, "move to 19:00")


def test_llm_unavailable_raised():
    """Simulated provider HTTP error raises LLMUnavailable."""
    fake_llm = FakeLLM(responses=[LLMUnavailable("Service unavailable")])
    budget = LLMBudget()

    with pytest.raises(LLMUnavailable):
        parse_message(fake_llm, budget, "move to 19:00")


def test_budget_exceeded_on_fifth_call():
    """Attempting a 5th LLM call raises LLMBudgetExceeded."""
    budget = LLMBudget()
    for _ in range(4):
        budget.consume()
    assert budget.calls == 4

    with pytest.raises(LLMBudgetExceeded):
        budget.consume()
