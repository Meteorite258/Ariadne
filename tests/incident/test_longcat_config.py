from pathlib import Path

from tau_ai.openai_compatible import _build_chat_payload
from tau_coding.catalog_loader import effective_catalog
from tau_coding.paths import TauPaths
from tau_coding.provider_config import load_provider_settings, provider_config_from_entry


def test_longcat_catalog_matches_documented_endpoint_and_output_field(tmp_path):
    source = Path(__file__).resolve().parents[2] / "examples/incident/longcat/catalog.toml"
    (tmp_path / "catalog.toml").write_bytes(source.read_bytes())
    entry = next(e for e in effective_catalog(TauPaths(home=tmp_path)) if e.name == "longcat")
    config = provider_config_from_entry(entry)
    assert (
        config.base_url + "/chat/completions"
        == "https://api.longcat.chat/openai/v1/chat/completions"
    )
    assert config.api_key_env == "LONGCAT_API_KEY"
    assert config.models == ("LongCat-2.5-Preview",)
    (tmp_path / "providers.json").write_bytes(source.with_name("providers.json").read_bytes())
    configured = load_provider_settings(TauPaths(home=tmp_path)).get_provider("longcat")
    assert configured.timeout_seconds == 180 and configured.max_retries == 0
    payload = _build_chat_payload(
        model=config.default_model,
        system="validation",
        messages=[],
        tools=[],
        max_tokens=4096,
        compat=config.compat,
        thinking_format=config.compat["thinkingFormat"],
    )
    assert payload["max_tokens"] == 4096
    assert payload["stream"] is True
    assert "max_completion_tokens" not in payload
    assert "store" not in payload
    assert "reasoning_effort" not in payload
    assert payload["thinking"] == {"type": "disabled"}
