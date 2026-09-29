import runpy
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "raw,kind,count",
    [
        (b'[{"latency": 42}]', "json", 1),
        (b'{"a":1}\n{"a":2}\n', "ndjson", 2),
        (b"a,b\n1,2\n3,4\n", "csv", 2),
        (b"a\tb\n1\t2\n", "tsv", 1),
        (b"opaque backend text", "text", None),
    ],
)
def test_evidence_formats_and_bounded_preview(raw, kind, count):
    helpers = runpy.run_path(
        str(Path(__file__).parents[2] / "examples/incident-demo/analysis/evidence.py")
    )
    describe = helpers["describe_evidence"]
    describe.__globals__["_read"] = lambda identity: raw
    assert describe("authorized")["format"] == kind
    assert describe("authorized")["rows"] == count
    assert len(describe("authorized")["sample"]) <= 2000
