"""Operator-only controls for Stage 7. Never exposed as an investigator tool."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from tau_incident.models import Scope
from tau_incident.telemetry import CatalogSnapshot
from tau_incident.telemetry.catalog import EnvironmentChange, VersionedCatalog

ROOT = Path(__file__).resolve().parent
COMMIT = "63649d6d6a59de88fb421b88c3c3a6185b6d21ad"


def now() -> datetime:
    return datetime.now(UTC)


def compose(*args: str, regression: bool = False) -> str:
    command = [
        "docker",
        "compose",
        "--env-file",
        str(ROOT / "demo.env"),
        "-f",
        str(ROOT / "upstream/docker-compose.yml"),
        "-f",
        str(ROOT / "compose.yaml"),
    ]
    if regression:
        command += ["-f", str(ROOT / "control/checkout-regression.yaml")]
    if (ROOT / "images.lock.json").exists():
        command += ["-f", str(ROOT / "images.lock.json")]
    return subprocess.run(
        [*command, *args], check=True, capture_output=True, text=True, timeout=180
    ).stdout


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def append_private(value: dict[str, Any]) -> None:
    path = ROOT / "control/actions.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"at": now().isoformat(), **value}) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def inspect_checkout() -> dict[str, Any]:
    identity = compose("ps", "--quiet", "checkout").strip()
    if not identity:
        raise ValueError("checkout must be deployed before applying a recorded change")
    data = json.loads(
        subprocess.run(
            ["docker", "inspect", identity], check=True, capture_output=True, text=True, timeout=15
        ).stdout
    )[0]
    environment = dict(item.split("=", 1) for item in data["Config"]["Env"] if "=" in item)
    return {
        "container_id": data["Id"],
        "image": data["Image"],
        "PAYMENT_ADDR": environment.get("PAYMENT_ADDR"),
        "running": data["State"]["Running"],
    }


def deploy(*, regression: bool) -> None:
    before = inspect_checkout()
    change_id = uuid4().hex
    append_private(
        {
            "operation": change_id,
            "type": "deployment",
            "status": "started",
            "before": before,
            "regression": regression,
        }
    )
    try:
        compose(
            "up",
            "-d",
            "--no-build",
            "--no-deps",
            "--force-recreate",
            "checkout",
            regression=regression,
        )
        after = inspect_checkout()
        expected = "payment-old:50051" if regression else "payment:50051"
        if after["PAYMENT_ADDR"] != expected or not after["running"]:
            raise ValueError("deployment postcondition not met")
        receipt = {
            "operation": change_id,
            "before": before,
            "after": after,
            "at": now().isoformat(),
            "status": "applied",
        }
        receipts = ROOT / "control/receipts"
        receipts.mkdir(exist_ok=True)
        (receipts / (change_id + ".json")).write_text(
            json.dumps(receipt, indent=2), encoding="utf-8"
        )
        catalog = VersionedCatalog(ROOT / "public/environment.json", clock=now)
        change = EnvironmentChange(
            change_id=change_id,
            kind="deployment",
            environment="amadeus-demo",
            entity="checkout",
            data_at=now(),
            available_at=now(),
            before_revision=digest(before),
            after_revision=digest(after),
            before=before,
            after=after,
            applied_receipt="sha256:" + digest(receipt),
        )
        catalog.append(change=change)
        publish_topology(regression=regression)
        append_private({"operation": change_id, "status": "recorded", "type": "deployment"})
    except BaseException as exc:
        append_private({"operation": change_id, "status": "unknown", "error": type(exc).__name__})
        raise


def publish_topology(*, regression: bool = False) -> None:
    resolved = json.loads(compose("config", "--format", "json", regression=regression))
    services = resolved["services"]
    allowed = json.loads((ROOT / "live.json").read_text())["live"]["services"]
    edges = set()
    configuration: dict[str, Any] = {}
    for name in allowed:
        value = services[name]
        endpoints = {
            key: value
            for key, value in value.get("environment", {}).items()
            if key.endswith("_ADDR")
        }
        configuration[name] = {"image": value["image"], "endpoints": endpoints}
        for endpoint in endpoints.values():
            if isinstance(endpoint, str):
                target = endpoint.removeprefix("http://").removeprefix("https://").split(":")[0]
                if target in allowed:
                    edges.add((name, target))
    snapshot = CatalogSnapshot(
        version=digest(configuration),
        scope=Scope(environment="amadeus-demo", entities=tuple(allowed)),
        available_at=now(),
        data_at=now(),
        entities=tuple(allowed),
        edges=tuple(sorted(edges)),
        configuration=configuration,
        origin="declared",
    )
    catalog = VersionedCatalog(ROOT / "public/environment.json", clock=now)
    catalog.append(snapshot=snapshot)


def flag(variant: str) -> None:
    path = ROOT / "upstream/src/flagd/demo.flagd.json"
    data = json.loads(path.read_text())
    previous = data["flags"]["paymentUnreachable"]["defaultVariant"]
    data["flags"]["paymentUnreachable"]["defaultVariant"] = variant
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(path)
    append_private(
        {
            "type": "feature_flag",
            "flag": "paymentUnreachable",
            "before": previous,
            "after": variant,
            "status": "file updated; flagd observation not confirmed",
        }
    )


def lock_images() -> None:
    resolved = json.loads(compose("config", "--format", "json"))
    images = {}
    for name, service in resolved["services"].items():
        inspected = json.loads(
            subprocess.run(
                ["docker", "image", "inspect", service["image"]],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout
        )[0]
        digests = inspected.get("RepoDigests", [])
        if not digests:
            raise ValueError("pull pinned release images before locking")
        images[name] = {"image": digests[0]}
    (ROOT / "images.lock.json").write_text(
        json.dumps({"services": images}, indent=2), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "start",
            "flag-on",
            "flag-off",
            "deploy-regression",
            "reset",
            "topology",
            "lock-images",
        ],
    )
    args = parser.parse_args()
    # An exclusive operator lock keeps concurrent controller updates from overwriting history.
    lock = ROOT / "control/operator.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        if args.action == "start":
            if not (ROOT / "images.lock.json").exists():
                raise ValueError("pull release images and run lock-images before starting")
            compose("up", "-d", "--no-build")
            publish_topology()
        elif args.action in {"flag-on", "flag-off"}:
            flag("on" if args.action == "flag-on" else "off")
        elif args.action in {"deploy-regression", "reset"}:
            if args.action == "reset":
                flag("off")
            deploy(regression=args.action == "deploy-regression")
        elif args.action == "lock-images":
            lock_images()
        else:
            publish_topology()
    finally:
        os.close(descriptor)
        lock.unlink()


if __name__ == "__main__":
    main()
