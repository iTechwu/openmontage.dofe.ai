from dataclasses import replace
from datetime import datetime, timedelta, timezone
from tools.dofe.mcp_delegation import McpDelegation, ModelsDelegationClient


def delegated(**kwargs):
    value = McpDelegation("a" * 64, "key-42", "tenant-42", "team-42", "req-42",
        ("models.list", "models.capability", "responses", "chat.completions", "generation.tasks.create",
         "generation.tasks.get", "generation.tasks.artifacts", "generation.tasks.cancel", "pricing.quote", "media.upload"),
        (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(), "request")
    return replace(value, **kwargs)


def stub_models(monkeypatch, value):
    state = {"value": value}
    def introspect(self, ref):
        assert ref == state["value"].delegation_ref
        return state["value"]
    def claim(self, current, job_id, source_invocation_id):
        assert current.api_key_id == state["value"].api_key_id
        state["value"] = replace(state["value"], mode="job", job_id=job_id)
        return state["value"]
    monkeypatch.setattr(ModelsDelegationClient, "introspect", introspect)
    monkeypatch.setattr(ModelsDelegationClient, "claim", claim)
    return state
