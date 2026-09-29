"""Exercise real incident daemon, remote CLI, and alert intake on WSL2 without a model."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx

from tau_coding.incident.settings import HostSettings


def _cli(uv: str, project: Path, config: Path, env: dict[str, str], *args: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            uv,
            "run",
            "--no-sync",
            "tau",
            "incident",
            *args,
            "--config",
            str(config),
            "--project",
            str(project),
            "--remote",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    parsed = json.loads(result.stdout)
    if not isinstance(parsed, dict):
        raise ValueError("incident CLI returned a non-object")
    return cast(dict[str, Any], parsed)


def _start(
    uv: str, project: Path, config: Path, env: dict[str, str], log: Path, url: str
) -> subprocess.Popen[bytes]:
    stream = log.open("a", encoding="utf-8")
    process = subprocess.Popen(
        [
            uv,
            "run",
            "--no-sync",
            "tau",
            "incident",
            "serve",
            "--config",
            str(config),
            "--project",
            str(project),
        ],
        cwd=project,
        env=env,
        stdout=stream,
        stderr=stream,
        start_new_session=True,
    )
    stream.close()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("incident daemon exited during startup")
        try:
            response = httpx.post(
                url + "/capabilities",
                headers={"Authorization": "Bearer " + env["AMADEUS_INCIDENT_TOKEN"]},
                timeout=1,
            )
            if response.status_code == 200:
                return process
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    _stop(process)
    raise TimeoutError("incident daemon did not start within 20 seconds")


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)  # type: ignore[attr-defined]
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
        process.wait(timeout=5)


def _updates(
    uv: str, project: Path, config: Path, env: dict[str, str], count: int
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        updates = _cli(uv, project, config, env, "inbox")["updates"]
        if len(updates) >= count and all(row["status"] == "processed" for row in updates[:count]):
            return cast(list[dict[str, Any]], updates)
        time.sleep(0.25)
    raise AssertionError("alert inbox did not finish association")


def _wait_update_status(
    uv: str,
    project: Path,
    config: Path,
    env: dict[str, str],
    inbox_id: str,
    status: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        updates = _cli(uv, project, config, env, "inbox")["updates"]
        for row in updates:
            if row["inbox_id"] == inbox_id and row["status"] == status:
                return cast(dict[str, Any], row)
        time.sleep(0.25)
    raise AssertionError(f"alert inbox did not reach {status}")


def main(output: Path) -> None:
    project = Path.cwd().resolve()
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv must be available on PATH")
    with tempfile.TemporaryDirectory(prefix="amadeus-stage7-product-") as directory:
        root = Path(directory)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        config = root / "host.json"
        config.write_text(
            HostSettings(environment="stage7-product", port=port, endpoint=url).model_dump_json(),
            encoding="utf-8",
        )
        env = dict(os.environ)
        env.pop("LONGCAT_API_KEY", None)
        env.update(
            TAU_HOME=str(root / "tau-home"),
            AMADEUS_INCIDENT_TOKEN=secrets.token_hex(24),
            AMADEUS_ALERT_TOKEN=secrets.token_hex(24),
        )
        log = root / "daemon.log"
        process = _start(uv, project, config, env, log, url)
        try:
            capabilities = _cli(uv, project, config, env, "connect")
            assert capabilities["environment"] == "stage7-product"
            start = datetime.now(UTC).replace(microsecond=0)
            alert = {
                "version": "4",
                "alerts": [
                    {
                        "status": "firing",
                        "fingerprint": "stage7-product-checkout",
                        "labels": {
                            "environment": "stage7-product",
                            "service": "checkout",
                            "alertname": "CheckoutErrors",
                            "severity": "critical",
                        },
                        "startsAt": start.isoformat(),
                    }
                ],
            }
            headers = {
                "Authorization": "Bearer " + env["AMADEUS_ALERT_TOKEN"],
                "X-Delivery-Id": "stage7-firing",
            }
            with httpx.Client(base_url=url, timeout=5) as client:
                first = client.post("/webhook/alertmanager", headers=headers, json=alert)
                assert first.status_code == 202 and not first.json()["duplicate"]
                duplicate = client.post("/webhook/alertmanager", headers=headers, json=alert)
                assert duplicate.status_code == 202 and duplicate.json()["duplicate"]
                changed = json.loads(json.dumps(alert))
                changed["alerts"][0]["annotations"] = {"summary": "changed delivery"}
                assert (
                    client.post("/webhook/alertmanager", headers=headers, json=changed).status_code
                    == 400
                )
                updates = _updates(uv, project, config, env, 1)
                case_id = updates[0]["case_id"]
                assert case_id
                brief = _cli(uv, project, config, env, "query", "--case-id", case_id)
                assert brief["case"]["case_id"] == case_id
                resolved = json.loads(json.dumps(alert))
                resolved["alerts"][0]["status"] = "resolved"
                resolved["alerts"][0]["endsAt"] = (start + timedelta(minutes=5)).isoformat()
                headers["X-Delivery-Id"] = "stage7-resolved"
                assert (
                    client.post("/webhook/alertmanager", headers=headers, json=resolved).status_code
                    == 202
                )
            updates = _updates(uv, project, config, env, 2)
            assert {row["case_id"] for row in updates[:2]} == {case_id}
            brief = _cli(uv, project, config, env, "query", "--case-id", case_id)
            assert brief["case"]["impact_status"] == "unknown"
            assert len(brief["case"]["observations"]) == 2
            unmatched = json.loads(json.dumps(alert))
            unmatched["alerts"][0]["status"] = "resolved"
            unmatched["alerts"][0]["fingerprint"] = "stage7-manual-association"
            unmatched["alerts"][0]["startsAt"] = (start + timedelta(hours=1)).isoformat()
            unmatched["alerts"][0]["endsAt"] = (start + timedelta(hours=1, minutes=5)).isoformat()
            headers["X-Delivery-Id"] = "stage7-manual"
            with httpx.Client(base_url=url, timeout=5) as client:
                delivered = client.post("/webhook/alertmanager", headers=headers, json=unmatched)
            assert delivered.status_code == 202
            pending = _wait_update_status(
                uv, project, config, env, delivered.json()["inbox_id"], "needs_association"
            )
            assert pending["case_id"] is None
            associated = _cli(
                uv,
                project,
                config,
                env,
                "associate",
                "--update-id",
                pending["update_id"],
                "--case-id",
                case_id,
            )
            assert associated["status"] == "pending"
            updates = _updates(uv, project, config, env, 3)
            assert {row["case_id"] for row in updates[:3]} == {case_id}
            brief = _cli(uv, project, config, env, "query", "--case-id", case_id)
            assert len(brief["case"]["observations"]) == 3
            assert brief["case"]["impact_status"] == "unknown"
            timeline = _cli(uv, project, config, env, "timeline", "--case-id", case_id)
            handoff = _cli(uv, project, config, env, "handoff", "--case-id", case_id)
            assert timeline["executions"] and handoff["case"]["case_id"] == case_id
            assert handoff["requests"] == []
        finally:
            _stop(process)
        process = _start(uv, project, config, env, log, url)
        try:
            restored = _cli(uv, project, config, env, "query", "--case-id", case_id)
            assert restored["case"]["case_id"] == case_id
            assert len(restored["case"]["observations"]) == 3
            imported = json.loads(json.dumps(alert))
            imported["alerts"][0]["fingerprint"] = "stage7-cli-import"
            imported["alerts"][0]["startsAt"] = (start + timedelta(hours=2)).isoformat()
            import_file = root / "cli-alert.json"
            import_file.write_text(json.dumps(imported), encoding="utf-8")
            imported_first = _cli(
                uv, project, config, env, "import-alert", "--json-file", str(import_file)
            )
            imported_again = _cli(
                uv, project, config, env, "import-alert", "--json-file", str(import_file)
            )
            assert not imported_first["duplicate"] and imported_again["duplicate"]
            updates = _updates(uv, project, config, env, 4)
            imported_case_id = next(
                row["case_id"] for row in updates if row["inbox_id"] == imported_first["inbox_id"]
            )
            assert imported_case_id and imported_case_id != case_id
            imported_case = _cli(uv, project, config, env, "query", "--case-id", imported_case_id)
            assert len(imported_case["case"]["observations"]) == 1
            imported_handoff = _cli(
                uv, project, config, env, "handoff", "--case-id", imported_case_id
            )
            assert imported_handoff["requests"] == []
            summary = {
                "case_id": case_id,
                "environment": "stage7-product",
                "firing_and_resolved": True,
                "duplicate_delivery": True,
                "changed_delivery_rejected": True,
                "manual_association": True,
                "cli_import_alert_duplicate": True,
                "cli_import_created_distinct_case": True,
                "observations": len(restored["case"]["observations"]),
                "impact_status": restored["case"]["impact_status"],
                "timeline_items": len(timeline["executions"]),
                "handoff_case_matches": True,
                "daemon_restart_preserved_case": True,
                "model_requests": len(handoff["requests"]),
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(summary))
        finally:
            _stop(process)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    main(args.output)
