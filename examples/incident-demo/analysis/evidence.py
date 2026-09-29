"""Helpers exposed by the trusted runner for this invocation's authorized mounts."""

import csv
import io
import json
from pathlib import Path


def _read(evidence_id):
    paths = json.loads(Path("/inputs/manifest.json").read_text())
    if evidence_id not in paths:
        raise ValueError("evidence was not authorized for this analysis")
    path = Path(paths[evidence_id])
    if (
        path.is_symlink()
        or path.resolve().parent != Path("/inputs")
        or path.name in {"script.py", "manifest.json"}
    ):
        raise ValueError("invalid evidence mount")
    return path.read_bytes()


def _decode(raw):
    text = raw.decode("utf-8-sig")
    try:
        return "json", json.loads(text)
    except ValueError:
        lines = text.splitlines()
        if lines:
            try:
                return "ndjson", [json.loads(line) for line in lines if line.strip()]
            except ValueError:
                pass
        if len(lines) > 1 and ("," in lines[0] or "\t" in lines[0]):
            delimiter = "\t" if "\t" in lines[0] else ","
            return "tsv" if delimiter == "\t" else "csv", list(
                csv.DictReader(io.StringIO(text), delimiter=delimiter)
            )
        return "text", text


def load_evidence(evidence_id):
    """Decode mounted JSON, NDJSON, CSV/TSV or UTF-8 text; unknown IDs fail closed."""
    return _decode(_read(evidence_id))[1]


def describe_evidence(evidence_id):
    """Bounded schema/sample preview; row count describes the stored payload only."""
    raw = _read(evidence_id)
    try:
        kind, value = _decode(raw)
    except UnicodeDecodeError:
        return {
            "format": "bytes",
            "size_bytes": len(raw),
            "fields": [],
            "rows": None,
            "sample": raw[:64].hex(),
        }
    rows = (
        value
        if isinstance(value, list)
        else value.get("rows", value.get("data", value))
        if isinstance(value, dict)
        else value
    )
    fields = (
        list(rows[0])[:50]
        if isinstance(rows, list) and rows and isinstance(rows[0], dict)
        else list(rows)[:50]
        if isinstance(rows, dict)
        else []
    )
    sample = json.dumps(rows[:3] if isinstance(rows, list) else rows, ensure_ascii=False)
    return {
        "format": kind,
        "size_bytes": len(raw),
        "fields": fields,
        "rows": len(rows) if isinstance(rows, list) else None,
        "sample": sample[:2000],
        "sample_truncated": len(sample) > 2000,
    }
