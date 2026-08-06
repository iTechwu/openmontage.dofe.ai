"""Job-scoped models delegation identity and request signing."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from dataclasses import dataclass, field
from uuid import uuid4


_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(frozen=True)
class DelegatedModelCredential:
    api_key: str = field(repr=False)
    models_base_url: str
    delegation_id: str
    external_job_id: str
    pipeline_stage: str
    runtime_credential_id: str
    expires_at: str

    def signed_headers(
        self,
        *,
        model_invocation_id: str | None = None,
        timestamp: int | None = None,
    ) -> dict[str, str]:
        invocation_id = model_invocation_id or f"om-{uuid4().hex}"
        for name, value in (
            ("delegation_id", self.delegation_id),
            ("external_job_id", self.external_job_id),
            ("pipeline_stage", self.pipeline_stage),
            ("model_invocation_id", invocation_id),
        ):
            if not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"Delegated model {name} is invalid")
        issued_at = str(timestamp if timestamp is not None else int(time.time()))
        signature = hmac.new(
            self.api_key.encode("utf-8"),
            "\n".join(
                [
                    self.delegation_id,
                    self.external_job_id,
                    self.pipeline_stage,
                    invocation_id,
                    issued_at,
                ]
            ).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "X-Dofe-Pipeline-Stage": self.pipeline_stage,
            "X-Dofe-Model-Invocation-Id": invocation_id,
            "X-Dofe-Attribution-Timestamp": issued_at,
            "X-Dofe-Attribution-Signature": signature,
        }

    def agent_environment(
        self,
        *,
        openai_base_url: str,
        dofe_base_url: str | None = None,
    ) -> dict[str, str]:
        return {
            "DOFE_ENABLED": "true",
            "DOFE_MODEL_API_KEY": self.api_key,
            "DOFE_MODEL_BASE_URL": dofe_base_url or self.models_base_url,
            "DOFE_DELEGATION_ID": self.delegation_id,
            "DOFE_EXTERNAL_JOB_ID": self.external_job_id,
            "DOFE_PIPELINE_STAGE": self.pipeline_stage,
            "DOFE_RUNTIME_CREDENTIAL_ID": self.runtime_credential_id,
            "DOFE_DELEGATION_EXPIRES_AT": self.expires_at,
            "OPENAI_API_KEY": self.api_key,
            "OPENAI_BASE_URL": openai_base_url,
        }


def delegated_credential_from_environment(
    *,
    api_key: str,
    models_base_url: str,
) -> DelegatedModelCredential | None:
    values = {
        "delegation_id": os.environ.get("DOFE_DELEGATION_ID", "").strip(),
        "external_job_id": os.environ.get("DOFE_EXTERNAL_JOB_ID", "").strip(),
        "pipeline_stage": os.environ.get("DOFE_PIPELINE_STAGE", "").strip(),
        "runtime_credential_id": os.environ.get("DOFE_RUNTIME_CREDENTIAL_ID", "").strip(),
        "expires_at": os.environ.get("DOFE_DELEGATION_EXPIRES_AT", "").strip(),
    }
    if not any(values.values()):
        return None
    required = ("delegation_id", "external_job_id", "pipeline_stage")
    if any(not values[name] for name in required):
        raise ValueError("Delegated models environment is incomplete")
    return DelegatedModelCredential(
        api_key=api_key,
        models_base_url=models_base_url,
        **values,
    )
