from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from tau_incident.coordinator import IncidentRuntime
from tau_incident.evidence import ArtifactStore, EvidenceRecorder
from tau_incident.execution import ExecutionRecorder
from tau_incident.store import CaseStore

from .test_store import observation


def test_duplicate_command_from_independent_connections_commits_once(runtime, case, clock):
    barrier = Barrier(2, timeout=10)
    command = observation(case, "concurrent")

    def submit():
        artifacts = ArtifactStore(runtime.store.artifacts.root)
        store = CaseStore(runtime.store.path, artifacts=artifacts, clock=clock)
        other = IncidentRuntime(
            store, EvidenceRecorder(artifacts), ExecutionRecorder(store, clock=clock), clock=clock
        )
        try:
            barrier.wait()
            return other.execute(command)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = [workers.submit(submit) for _ in range(2)]
        receipts = [job.result(timeout=15) for job in pending]
    assert receipts[0] == receipts[1]
    assert receipts[0].status == "accepted"
    state = runtime.get_case("case")
    assert state.version == 2 and len(state.observations) == 1
    assert len(runtime.store.events("case")) == 2
    assert runtime.store.read_evidence("case", state.observations[0].evidence_id) == b"3 errors"
    assert runtime.store.case_at_version("case", state.version) == state
