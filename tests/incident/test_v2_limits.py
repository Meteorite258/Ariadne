import pytest
from pydantic import ValidationError

from tau_incident.budget import RunLimits


def test_new_cases_are_unlimited_unless_operator_sets_policy():
    assert RunLimits().token_limit is None
    assert RunLimits(token_limit=100).token_limit == 100
    assert RunLimits(checkpoint_steps=8).checkpoint_steps == 8


@pytest.mark.parametrize(
    "name",
    [
        "max_turns",
        "role_timeout_seconds",
        "max_tasks",
        "call_limit",
        "report_reserve_calls",
        "report_reserve_tokens",
    ],
)
def test_removed_limits_have_actionable_errors(name):
    with pytest.raises(ValidationError, match="checkpoint_steps"):
        RunLimits.model_validate({name: 10})
