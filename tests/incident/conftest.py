from datetime import UTC, datetime, timedelta

import pytest

from tau_incident.coordinator import IncidentRuntime
from tau_incident.events import Command, CreateCase
from tau_incident.evidence import ArtifactStore, EvidenceRecorder
from tau_incident.execution import ExecutionRecorder
from tau_incident.models import Scope, Source
from tau_incident.store import CaseStore


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 27, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def runtime(tmp_path, clock):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    store = CaseStore(tmp_path / "cases.sqlite", artifacts=artifacts, clock=clock)
    runtime = IncidentRuntime(
        store, EvidenceRecorder(artifacts), ExecutionRecorder(store, clock=clock), clock=clock
    )
    yield runtime
    store.close()


@pytest.fixture
def create_command():
    return Command(
        command_id="create",
        case_id="case",
        payload=CreateCase(
            project_key="validation",
            scope=Scope(environment="test", entities=("checkout",)),
            symptoms="checkout fails",
            impact="orders fail",
            source=Source(kind="human", actor="operator"),
        ),
    )


@pytest.fixture
def case(runtime, create_command):
    assert runtime.execute(create_command).status == "accepted"
    return runtime.get_case("case")
