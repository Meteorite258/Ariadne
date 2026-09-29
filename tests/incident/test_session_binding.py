import pytest

from pi_event_helpers import assistant_done, assistant_start
from tau_agent import AssistantMessage, UserMessage
from tau_agent.session import CustomEntry, JsonlSessionStorage, MessageEntry
from tau_ai import FakeProvider
from tau_coding import CodingSession, CodingSessionConfig, SessionManager, TauPaths
from tau_coding.incident.session import NAMESPACE, bind, binding
from tau_coding.provider_config import OpenAICompatibleProviderConfig, ProviderSettings

from .test_store import observation


@pytest.mark.anyio
async def test_persistent_binding_new_chat_and_resume_never_roll_back_case(
    runtime, case, tmp_path, monkeypatch
):
    manager = SessionManager(TauPaths(home=tmp_path / "tau", agents_home=tmp_path / "agents"))
    record = manager.create_session(cwd=tmp_path, model="fake")

    class OwnedFakeProvider(FakeProvider):
        async def aclose(self):
            pass

    provider = OwnedFakeProvider([])
    monkeypatch.setattr(
        "tau_coding.session.create_model_provider", lambda *args, **kwargs: provider
    )
    settings = ProviderSettings(
        default_provider="openai",
        providers=(
            OpenAICompatibleProviderConfig(name="openai", models=("fake",), default_model="fake"),
        ),
    )
    config = CodingSessionConfig(
        provider=provider,
        model="fake",
        system="test",
        storage=JsonlSessionStorage(record.path),
        cwd=tmp_path,
        session_id=record.id,
        session_manager=manager,
        provider_settings=settings,
    )
    session = await CodingSession.load(config)
    await bind(session, "case")
    assert await binding(session) == "case"
    await session.aclose()
    assert runtime.execute(observation(case, "after-chat-close")).status == "accepted"
    current = runtime.get_case("case")
    restored = await CodingSession.load(config)
    try:
        assert await binding(restored) == "case"
        await restored.new_session()
        assert await binding(restored) is None
        assert runtime.get_case("case") == current
        await restored.resume(record.id)
        assert await binding(restored) == "case"
        assert runtime.get_case("case") == current
        assert provider.calls == []
    finally:
        await restored.aclose()


@pytest.mark.anyio
async def test_branch_and_compaction_only_change_chat_not_committed_case(
    runtime, case, tmp_path, monkeypatch
):
    monkeypatch.setattr("tau_coding.session.DEFAULT_COMPACTION_KEEP_RECENT_TOKENS", 1)
    storage = JsonlSessionStorage(tmp_path / "branch.jsonl")
    entries = (
        CustomEntry(id="binding", namespace=NAMESPACE, data={"active_case_id": "case"}),
        MessageEntry(
            id="question", parent_id="binding", message=UserMessage(content="Review case")
        ),
        MessageEntry(
            id="answer", parent_id="question", message=AssistantMessage(content="Old chat")
        ),
        CustomEntry(
            id="unbound", parent_id="answer", namespace=NAMESPACE, data={"active_case_id": None}
        ),
        MessageEntry(
            id="other", parent_id="unbound", message=UserMessage(content="Unrelated chat")
        ),
    )
    for entry in entries:
        await storage.append(entry)
    provider = FakeProvider(
        [
            [
                assistant_start(model="fake"),
                assistant_done(
                    message=AssistantMessage(content="Chat summary; consult current case records.")
                ),
            ]
        ]
    )
    config = CodingSessionConfig(
        provider=provider,
        model="fake",
        system="test",
        storage=storage,
        cwd=tmp_path,
    )
    session = await CodingSession.load(config)
    try:
        assert await binding(session) is None
        assert runtime.execute(observation(case, "committed-after-chat")).status == "accepted"
        current = runtime.get_case("case")
        await session.branch_to_entry("answer")
        assert await binding(session) == "case"
        assert runtime.get_case("case") == current
        await session.compact()
        assert await binding(session) == "case"
        assert runtime.get_case("case") == current
        saved = await storage.read_all()
        assert any(entry.type == "compaction" for entry in saved)
        assert all(entry in saved for entry in entries)
        assert runtime.store.case_at_version("case", current.version) == current
    finally:
        await session.aclose()
    resumed = await CodingSession.load(config)
    try:
        assert await binding(resumed) == "case"
        assert runtime.get_case("case") == current
        assert len(provider.calls) == 1
    finally:
        await resumed.aclose()
