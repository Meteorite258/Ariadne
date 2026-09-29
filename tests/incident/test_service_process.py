import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import time

import httpx

from tau_coding.incident.actions import IncidentAction
from tau_coding.incident.settings import HostSettings
from tau_coding.paths import TauPaths
from tau_incident.events import AddConstraint, Command
from tau_incident.models import Source


def test_daemon_survives_sse_disconnect_and_process_restart(tmp_path, create_command):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    settings = HostSettings(environment="test", port=port, endpoint=endpoint)
    config = tmp_path / "host.json"
    config.write_text(settings.model_dump_json(), encoding="utf-8")
    token = secrets.token_hex(24)
    environment = {
        **os.environ,
        "TAU_HOME": str(tmp_path / "tau"),
        "AMADEUS_INCIDENT_TOKEN": token,
        "AMADEUS_ALERT_TOKEN": secrets.token_hex(24),
    }
    uv = shutil.which("uv")
    assert uv, "Run this test through uv with uv available on PATH"
    command = [
        uv,
        "run",
        "--no-sync",
        "tau",
        "incident",
        "serve",
        "--config",
        str(config),
        "--project",
        str(tmp_path),
    ]
    create_command = create_command.model_copy(
        update={
            "payload": create_command.payload.model_copy(
                update={
                    "project_key": TauPaths(home=tmp_path / "tau")
                    .project_incident_dir(tmp_path)
                    .name
                }
            )
        }
    )
    with (tmp_path / "daemon.log").open("w", encoding="utf-8") as log:
        process = None

        def start():
            child = subprocess.Popen(
                command,
                env=environment,
                stdout=log,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                assert child.poll() is None, "daemon exited during startup; inspect daemon.log"
                try:
                    response = httpx.post(
                        endpoint + "/capabilities",
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=1,
                    )
                    if response.status_code == 200:
                        return child
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            terminate(child)
            child.wait(timeout=5)
            raise AssertionError("daemon did not become ready")

        def terminate(child):
            if os.name == "nt":
                subprocess.run(
                    ["taskkill.exe", "/PID", str(child.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                )
            else:
                os.killpg(child.pid, signal.SIGKILL)

        def stop():
            if process is not None and process.poll() is None:
                terminate(process)
                process.wait(timeout=10)

        try:
            process = start()
            with httpx.Client(
                base_url=endpoint, headers={"Authorization": f"Bearer {token}"}, timeout=10
            ) as client:
                action = IncidentAction(
                    request_id="create", operation="command", case_id="case", command=create_command
                )
                created = client.post("/actions", json=action.model_dump(mode="json"))
                assert created.status_code == 200
                assert created.json()["receipt"]["status"] == "accepted"
                with client.stream("POST", "/events", json={"case_id": "case"}) as stream:
                    assert stream.status_code == 200
                    page = next(
                        json.loads(line[6:])
                        for line in stream.iter_lines()
                        if line.startswith("data: ")
                    )
                    cursor = page["next_cursor"]
                    assert page["events"]
                assert process.poll() is None
                constraint = Command(
                    command_id="constraint",
                    case_id="case",
                    payload=AddConstraint(
                        text="Read-only investigation",
                        scope=create_command.payload.scope,
                        source=Source(kind="human", actor="operator"),
                    ),
                )
                added = client.post(
                    "/actions",
                    json=IncidentAction(
                        request_id="constraint",
                        operation="command",
                        case_id="case",
                        command=constraint,
                    ).model_dump(mode="json"),
                )
                assert added.status_code == 200 and added.json()["receipt"]["status"] == "accepted"
                stop()
                process = start()
                repeated = client.post("/actions", json=action.model_dump(mode="json"))
                assert repeated.json() == created.json()
                with client.stream(
                    "POST", "/events", json={"case_id": "case", "after_cursor": cursor}
                ) as stream:
                    resumed = next(
                        json.loads(line[6:])
                        for line in stream.iter_lines()
                        if line.startswith("data: ")
                    )
                assert len(resumed["events"]) == 1
                assert resumed["next_cursor"] > cursor
        finally:
            stop()
