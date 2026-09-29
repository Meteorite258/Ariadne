"""Load trusted helpers explicitly: isolated Python ignores PYTHONPATH."""

import runpy

helpers = runpy.run_path("/evidence.py")
runpy.run_path(
    "/inputs/script.py",
    run_name="__main__",
    init_globals={
        "load_evidence": helpers["load_evidence"],
        "describe_evidence": helpers["describe_evidence"],
    },
)
