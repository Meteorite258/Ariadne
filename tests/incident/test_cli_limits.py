from tau_coding.incident.cli import _parser


def test_incident_run_defaults_to_no_call_ceiling_or_checkpoint():
    args = _parser().parse_args(
        ["run", "case", "--environment", "demo", "--fixture", "replay.json"]
    )
    assert args.token_limit is None
    assert not hasattr(args, "call_limit")
    assert args.checkpoint_steps is None
    assert not hasattr(args, "max_tasks")
    assert not hasattr(args, "max_turns")


def test_incident_resume_accepts_optional_step_checkpoint():
    args = _parser().parse_args(
        [
            "resume",
            "case",
            "--environment",
            "demo",
            "--fixture",
            "replay.json",
            "--checkpoint-steps",
            "8",
        ]
    )
    assert args.checkpoint_steps == 8
    assert args.token_limit is None
    assert not hasattr(args, "call_limit")
