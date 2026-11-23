"""Base tool class implementing the expanded ToolContract.

Every tool in OpenMontage inherits from BaseTool. This enforces a uniform
interface for discovery, execution, cost estimation, and health reporting.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import os
import platform
import subprocess
import shutil
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional


def _load_dotenv() -> None:
    """Load .env into os.environ once at import time.

    This ensures API keys are available before any tool is instantiated,
    even when tools are imported directly without going through the registry.
    Only sets variables that are not already in the environment.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    import re

    with open(env_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Quoted value: take the content inside the quotes verbatim.
            if value[:1] in ("'", '"'):
                quote = value[0]
                end = value.find(quote, 1)
                value = value[1:end] if end != -1 else value[1:]
            else:
                # Strip an inline comment ('#' at line start or after
                # whitespace) so "VAR=   # note" yields "" not "# note".
                match = re.search(r"(^|\s)#", value)
                if match:
                    value = value[: match.start()]
                value = value.strip()
            if key and (key not in os.environ or not os.environ[key].strip()):
                os.environ[key] = value


_load_dotenv()


class ToolTier(str, Enum):
    CORE = "core"
    VOICE = "voice"
    ENHANCE = "enhance"
    GENERATE = "generate"
    SOURCE = "source"
    ANALYZE = "analyze"
    PUBLISH = "publish"


class ToolStability(str, Enum):
    EXPERIMENTAL = "experimental"
    BETA = "beta"
    PRODUCTION = "production"


class ToolStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DEGRADED = "degraded"


class ToolRuntime(str, Enum):
    """Where and how a tool executes."""

    LOCAL = "local"  # Runs entirely on-device, free, no network
    LOCAL_GPU = "local_gpu"  # Runs on-device but needs GPU (VRAM)
    API = "api"  # Calls an external API, requires API key, costs money
    HYBRID = "hybrid"  # Can run locally OR via API (e.g., image_selector)


class ExecutionMode(str, Enum):
    SYNC = "sync"
    ASYNC = "async"


class Determinism(str, Enum):
    DETERMINISTIC = "deterministic"
    SEEDED = "seeded"
    STOCHASTIC = "stochastic"


class ResumeSupport(str, Enum):
    NONE = "none"
    FROM_START = "from_start"
    FROM_CHECKPOINT = "from_checkpoint"


@dataclass
class ResourceProfile:
    """Hardware resource envelope for a tool."""

    cpu_cores: int = 1
    ram_mb: int = 512
    vram_mb: int = 0
    disk_mb: int = 100
    network_required: bool = False


@dataclass
class RetryPolicy:
    """Safe retry behavior for a tool."""

    max_retries: int = 0
    backoff_seconds: float = 1.0
    retryable_errors: list[str] = field(default_factory=list)


@dataclass
class ToolResult:
    """Standard result returned by tool execution."""

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    error: Optional[str] = None
    cost_usd: Optional[float] = 0.0
    # Native provider billing. Keep cost_usd for legacy USD-only consumers,
    # but never put CNY or an unknown currency into it.
    cost_amount: Optional[float] = None
    cost_currency: Optional[str] = None
    cost_source: Optional[str] = None
    duration_seconds: float = 0.0
    seed: Optional[int] = None
    model: Optional[str] = None
    from_cache: bool = False


import threading as _threading

# Shared nesting counter for instrumented execute() calls (thread-local so
# parallel tool threads don't see each other's depth).
_EXECUTE_DEPTH = _threading.local()


def _instrument_execute(fn: Callable) -> Callable:
    """Wrap a tool's execute() with Backlot event emission.

    Appends start/finish/error entries to the owning project's events.jsonl
    when the call can be attributed to a project (explicit project_dir input
    or any path input under projects/). Powers the board's live activity
    ticker and per-scene generating states with zero agent involvement.

    Instrumentation is strictly non-fatal: any failure inside the event layer
    is swallowed and the tool call proceeds untouched.
    """
    if getattr(fn, "_backlot_instrumented", False):
        return fn

    depth_state = _EXECUTE_DEPTH  # shared across all tools (selector → provider)

    @functools.wraps(fn)
    def wrapper(self, inputs: Any, *args: Any, **kwargs: Any):
        from tools.dofe.config import model_api_policy_error

        policy_error = model_api_policy_error(self)
        if policy_error is not None:
            return ToolResult(success=False, error=policy_error)

        # Event layer is fully optional: if it can't import, run untouched.
        try:
            from lib.events import emit_event, infer_project_dir
        except Exception:
            return fn(self, inputs, *args, **kwargs)

        tool_name = getattr(self, "name", "") or self.__class__.__name__
        scene_id = inputs.get("scene_id") if isinstance(inputs, dict) else None
        output_path = inputs.get("output_path") if isinstance(inputs, dict) else None
        # Nesting depth: selector tools delegate to provider tools' execute().
        # Both emit (the ticker wants the provider name too), but depth lets
        # consumers dedupe — e.g. sum cost_usd only at depth 0.
        depth = getattr(depth_state, "value", 0)
        depth_state.value = depth + 1
        project_dir = infer_project_dir(inputs)

        base = {
            "call_id": uuid.uuid4().hex,
            "tool": tool_name,
            "scene_id": scene_id,
            "depth": depth if depth else None,
        }
        if project_dir is not None:
            emit_event(
                project_dir,
                {
                    **base,
                    "event": "start",
                    "output_path": str(output_path) if output_path else None,
                },
            )

        started = time.monotonic()
        try:
            result = fn(self, inputs, *args, **kwargs)
        except Exception as exc:
            if project_dir is not None:
                emit_event(
                    project_dir,
                    {
                        **base,
                        "event": "error",
                        "error": str(exc)[:300],
                        "duration_s": round(time.monotonic() - started, 2),
                    },
                )
            raise
        finally:
            depth_state.value = depth

        if project_dir is None:
            # The tool may have created its own project dir during execute
            # (first call of a run) — attribute the finish if possible.
            project_dir = infer_project_dir(inputs)
        if project_dir is not None:
            cost = getattr(result, "cost_usd", None)
            cost_amount = getattr(result, "cost_amount", None)
            emit_event(
                project_dir,
                {
                    **base,
                    "event": "finish",
                    "output_path": str(output_path) if output_path else None,
                    "success": getattr(result, "success", None),
                    # NOTE: 0.0 is meaningful (ran for free) — only None is dropped.
                    "cost_usd": cost if isinstance(cost, (int, float)) else None,
                    "cost_amount": (
                        cost_amount if isinstance(cost_amount, (int, float)) else None
                    ),
                    "cost_currency": getattr(result, "cost_currency", None),
                    "cost_source": getattr(result, "cost_source", None),
                    "duration_s": round(time.monotonic() - started, 2),
                },
            )
        return result

    wrapper._backlot_instrumented = True  # type: ignore[attr-defined]
    return wrapper


_FILE_HASH_CHUNK = 1024 * 1024


def _hash_file(path: Path) -> Optional[str]:
    """SHA-256 of a file's bytes, read in chunks. None if it can't be read."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(_FILE_HASH_CHUNK), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _fingerprint_value(value: Any) -> Any:
    """Make one cache-key value content-addressed.

    A value that points at an existing file is replaced by a hash of the file's
    bytes, so rewriting the file at the same path changes the key. A path string
    on its own would not, which would let a rerun restore output derived from
    stale input bytes. Lists and dicts are walked so nested asset paths are
    covered too; everything else passes through unchanged.
    """
    if isinstance(value, (str, os.PathLike)):
        try:
            path = Path(value)
            if path.is_file():
                digest = _hash_file(path)
                if digest is not None:
                    return {"__file_sha256__": digest}
        except (OSError, ValueError):
            pass
        return value
    if isinstance(value, (list, tuple)):
        return [_fingerprint_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _fingerprint_value(v) for k, v in value.items()}
    return value


def _is_cacheable(tool: "BaseTool", inputs: dict[str, Any]) -> bool:
    """Whether a call may be served from / written to the asset cache.

    Cache only reproducible, single-output calls whose sole side effect is the
    file they write: a declared idempotency contract, a concrete output_path to
    materialize into, not a publisher, and a determinism class that guarantees
    the same bytes: deterministic always, seeded only when a seed is pinned
    (an unpinned seed is effectively stochastic), stochastic never.
    """
    # A documented preflight must reach the tool's dry-run branch without
    # materializing an artifact or touching the cache's access metadata.
    if inputs.get("dry_run") and "dry_run" in tool.input_schema.get("properties", {}):
        return False
    if not tool.idempotency_key_fields:
        return False
    if not inputs.get("output_path"):
        return False
    if tool.tier == ToolTier.PUBLISH:
        return False
    det = tool.determinism
    if det == Determinism.STOCHASTIC:
        return False
    if det == Determinism.SEEDED and inputs.get("seed") is None:
        return False
    return True


def _result_from_cache(restored: dict[str, Any], dest: Any) -> "ToolResult":
    """Build a ToolResult for a cache hit that was materialized to ``dest``."""
    data = dict(restored.get("data") or {})
    # Repoint only the declared local-output references. Cache admission is
    # single-artifact; remote URLs, input paths, and other provenance stay put.
    original_outputs = {
        data[name] for name in ("output", "output_path")
        if isinstance(data.get(name), str)
    }
    for name in ("outputs", "video_paths", "image_paths", "audio_paths"):
        values = data.get(name)
        if (isinstance(values, list) and len(values) == 1
                and isinstance(values[0], str) and values[0] in original_outputs):
            data[name] = [str(dest)]
    for name in ("output", "output_path"):
        if name in data:
            data[name] = str(dest)
    data["cached"] = True
    return ToolResult(
        success=True,
        data=data,
        artifacts=[str(dest)],
        cost_usd=0.0,
        duration_seconds=0.0,
        seed=restored.get("seed"),
        model=restored.get("model"),
        from_cache=True,
    )


def _cache_execute(fn: Callable) -> Callable:
    """Wrap a tool's execute() with content-addressed output caching.

    When the asset cache is enabled and the call is reproducible, a repeat of a
    prior call links the cached artifact back into the requested output path and
    returns a free ToolResult without invoking fn or paying a provider. On a
    miss, fn runs and a single-file success is ingested for next time.

    The cache layer is strictly optional and non-fatal: any failure importing or
    using it falls through to the live call untouched. It is also off unless
    OPENMONTAGE_ASSET_CACHE is set, so an existing run behaves exactly as before.
    """
    if getattr(fn, "_asset_cached", False):
        return fn

    @functools.wraps(fn)
    def wrapper(self, inputs: Any, *args: Any, **kwargs: Any):
        try:
            from lib.asset_cache import asset_cache_enabled, get_default_asset_cache
        except Exception:
            return fn(self, inputs, *args, **kwargs)

        if not asset_cache_enabled() or not isinstance(inputs, dict):
            return fn(self, inputs, *args, **kwargs)
        if not _is_cacheable(self, inputs):
            return fn(self, inputs, *args, **kwargs)

        try:
            key = self.cache_key(inputs)
            cache = get_default_asset_cache()
        except Exception:
            return fn(self, inputs, *args, **kwargs)

        dest = inputs["output_path"]
        try:
            restored = cache.try_restore(key, Path(dest))
        except Exception:
            restored = None
        if restored is not None:
            return _result_from_cache(restored, dest)

        result = fn(self, inputs, *args, **kwargs)

        try:
            artifacts = getattr(result, "artifacts", None) or []
            if getattr(result, "success", False) and len(artifacts) == 1:
                art = Path(artifacts[0])
                if art.exists():
                    cache.store(
                        key,
                        art,
                        tool_name=self.name,
                        tool_version=self.version,
                        model=getattr(result, "model", None) or "",
                        seed=getattr(result, "seed", None),
                        cost_usd=float(getattr(result, "cost_usd", 0.0) or 0.0),
                        data=getattr(result, "data", None),
                    )
        except Exception:
            pass
        return result

    wrapper._asset_cached = True  # type: ignore[attr-defined]
    return wrapper


class BaseTool(ABC):
    """Abstract base class for all OpenMontage tools."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Auto-instrument every concrete execute() with Backlot events."""
        super().__init_subclass__(**kwargs)
        impl = cls.__dict__.get("execute")
        if impl is not None and not getattr(impl, "__isabstractmethod__", False):
            # Cache is outermost: a hit skips both the live call and its event
            # emission. Instrumentation wraps the raw implementation beneath it.
            cls.execute = _cache_execute(_instrument_execute(impl))

    # --- Identity (override in subclasses) ---
    name: str = ""
    version: str = "0.1.0"
    tier: ToolTier = ToolTier.CORE
    stability: ToolStability = ToolStability.EXPERIMENTAL
    execution_mode: ExecutionMode = ExecutionMode.SYNC
    determinism: Determinism = Determinism.DETERMINISTIC
    runtime: ToolRuntime = ToolRuntime.LOCAL

    # --- Dependencies ---
    # For API tools, add "env:ENVVAR_NAME" to signal required API keys
    dependencies: list[str] = []
    install_instructions: str = ""

    # --- Capabilities ---
    capability: str = "generic"
    provider: str = "openmontage"
    capabilities: list[str] = []
    input_schema: dict = {}
    output_schema: dict = {}
    artifact_schema: dict = {}
    progress_schema: Optional[dict] = None
    supports: dict[str, Any] = {}
    # Provider-surface reference contract. Keep this separate from generic
    # ``supports`` so agents can distinguish input parameters from prompt-token
    # syntax instead of assuming one provider's notation works everywhere.
    reference_binding_contract: dict[str, Any] = {}
    best_for: list[str] = []
    not_good_for: list[str] = []
    provider_matrix: dict[str, Any] = {}

    # --- Resource & retry ---
    resource_profile: ResourceProfile = ResourceProfile()
    retry_policy: RetryPolicy = RetryPolicy()

    # --- Resume & idempotency ---
    resume_support: ResumeSupport = ResumeSupport.NONE
    idempotency_key_fields: list[str] = []

    # --- Side effects & fallback ---
    side_effects: list[str] = []
    fallback: Optional[str] = None
    fallback_tools: list[str] = []

    # --- Agent skills (Layer 3 references) ---
    # Names of installed agent skills in .agents/skills/ that teach the
    # underlying technology. The orchestrator uses these to load relevant
    # API knowledge when planning tool usage.
    agent_skills: list[str] = []

    def agent_skills_for(self, inputs: Optional[dict[str, Any]] = None) -> list[str]:
        """Return Layer 3 skills for the concrete operation/model.

        Most tools have one stable skill set. Multi-model gateways may override
        this method so the selector does not expose Seedance guidance when a
        different model family was explicitly selected.
        """
        return list(self.agent_skills)

    # --- Verification ---
    user_visible_verification: list[str] = []

    # --- Optional telemetry / quality hints for the scoring engine ---
    # If set (0.0-1.0), lib/scoring.py uses these directly instead of falling
    # back to stability-based heuristics. Leave unset unless the tool has a
    # real measured or well-calibrated value.
    quality_score: Optional[float] = None
    historical_success_rate: Optional[float] = None
    latency_p50_seconds: Optional[float] = None

    # ---- Status reporting ----

    def get_status(self) -> ToolStatus:
        """Check if this tool's dependencies are satisfied."""
        try:
            self.check_dependencies()
            return ToolStatus.AVAILABLE
        except DependencyError:
            return ToolStatus.UNAVAILABLE

    def check_dependencies(self) -> None:
        """Verify all dependencies are installed. Raises DependencyError if not."""
        for dep in self.dependencies:
            if dep.startswith(("cmd:", "binary:")):
                prefix = "cmd:" if dep.startswith("cmd:") else "binary:"
                cmd_name = dep[len(prefix) :]
                if shutil.which(cmd_name) is None:
                    raise DependencyError(
                        f"Command {cmd_name!r} not found. {self.install_instructions}"
                    )
            elif dep.startswith("env:"):
                env_names = [name.strip() for name in dep[4:].split("|") if name.strip()]
                # Whitespace-only values count as unset (upstream contract).
                if not any((os.environ.get(name) or "").strip() for name in env_names):
                    env_label = " or ".join(repr(name) for name in env_names)
                    raise DependencyError(
                        f"Environment variable {env_label} not set. {self.install_instructions}"
                    )
            elif dep.startswith("python:"):
                module_name = dep[7:]
                try:
                    __import__(module_name)
                except ImportError:
                    raise DependencyError(
                        f"Python module {module_name!r} not installed. {self.install_instructions}"
                    )

    def get_info(self) -> dict[str, Any]:
        """Return full tool contract info for registry/discovery."""
        usage_location = inspect.getfile(self.__class__)
        default_agent_skills = self.agent_skills_for()
        return {
            "name": self.name,
            "version": self.version,
            "tier": self.tier.value,
            "capability": self.capability,
            "provider": self.provider,
            "stability": self.stability.value,
            "status": self.get_status().value,
            "execution_mode": self.execution_mode.value,
            "determinism": self.determinism.value,
            "runtime": self.runtime.value,
            "module_path": self.__class__.__module__,
            "usage_location": usage_location,
            "dependencies": self.dependencies,
            "install_instructions": self.install_instructions,
            "capabilities": self.capabilities,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "artifact_schema": self.artifact_schema,
            "supports": self.supports,
            "reference_binding_contract": self.reference_binding_contract,
            "best_for": self.best_for,
            "not_good_for": self.not_good_for,
            "provider_matrix": self.provider_matrix,
            "resource_profile": {
                "cpu_cores": self.resource_profile.cpu_cores,
                "ram_mb": self.resource_profile.ram_mb,
                "vram_mb": self.resource_profile.vram_mb,
                "disk_mb": self.resource_profile.disk_mb,
                "network_required": self.resource_profile.network_required,
            },
            "resume_support": self.resume_support.value,
            "side_effects": self.side_effects,
            "fallback": self.fallback,
            "fallback_tools": self.fallback_tools or ([self.fallback] if self.fallback else []),
            "agent_skills": default_agent_skills,
            "related_skills": default_agent_skills,
            "user_visible_verification": self.user_visible_verification,
            "quality_score": self.quality_score,
            "historical_success_rate": self.historical_success_rate,
            "latency_p50_seconds": self.latency_p50_seconds,
        }

    # ---- Cost estimation ----

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        """Estimate cost in USD for the given inputs. Override for paid tools."""
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        """Estimate runtime in seconds. Override for long-running tools."""
        return 0.0

    # ---- Idempotency ----

    def idempotency_key(self, inputs: dict[str, Any]) -> str:
        """Compute a cache key from idempotency fields."""
        key_data = {k: inputs.get(k) for k in self.idempotency_key_fields}
        raw = json.dumps(key_data, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def cache_key(self, inputs: dict[str, Any]) -> str:
        """Content-addressed key for this call, used by the asset cache.

        Combines tool identity and version with the declared
        ``idempotency_key_fields`` so identical inputs to the same tool version
        resolve to the same key, while a version bump or a different tool never
        collides. Returns the full SHA-256 hex; the cache stores blobs under it.

        File-valued fields are fingerprinted by content, not by path string, so
        rewriting an input file at the same path changes the key rather than
        serving output derived from the old bytes. For tools that exclude output
        paths from this set, placement alone does not change the key. The normalized
        destination suffix is included because local encoders select their
        output container from it; moving between formats must render again.
        """
        key_data = {
            "tool": self.name,
            "version": self.version,
            "output_suffix": Path(inputs.get("output_path") or "").suffix.lower(),
            "fields": {
                k: _fingerprint_value(inputs.get(k))
                for k in self.idempotency_key_fields
            },
        }
        raw = json.dumps(key_data, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    # ---- Execution ----

    @abstractmethod
    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """Run the tool. Subclasses must implement this."""
        ...

    def dry_run(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Preflight check without side effects. Override for paid/publishing tools."""
        from tools.provider_pricing import PriceQuoteRequired

        quote_error = None
        try:
            estimated_cost = self.estimate_cost(inputs)
        except PriceQuoteRequired as exc:
            estimated_cost = None
            quote_error = str(exc)
        return {
            "tool": self.name,
            "estimated_cost_usd": estimated_cost,
            "cost_status": "quote_required" if quote_error else "estimated",
            "quote_reason": quote_error,
            "estimated_runtime_seconds": self.estimate_runtime(inputs),
            "status": self.get_status().value,
            "would_execute": True,
        }

    def probe_provider_contract(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Probe a provider's live, side-effect-free contract when supported.

        Provider tools may override this to query authoritative model metadata
        or an input schema without submitting a paid generation. The default is
        deliberately honest: dependency availability does not prove live model
        entitlement or a current remote interface. Overrides fetch any tenant
        catalog themselves through the authenticated entry point — a caller must
        never be able to supply a (potentially forged) catalog here.
        """
        return {
            "status": "not_supported",
            "verification_scope": [],
            "warnings": [
                "Provider exposes no side-effect-free live contract probe; "
                "model access remains unverified until a representative sample succeeds."
            ],
            "errors": [],
        }

    def preflight(self, inputs: dict[str, Any], *, live: bool = False) -> dict[str, Any]:
        """Validate declared inputs and optionally probe the live provider contract.

        This method reports execution facts only. It does not choose a provider,
        approve a creative prompt, or decide whether a continuity deviation is
        acceptable; those remain agent/reviewer responsibilities.
        """
        from tools.dofe.config import model_api_policy_error

        policy_error = model_api_policy_error(self)
        if policy_error is not None:
            return {
                "status": "blocked",
                "verification_level": "declared_tool_contract",
                "tool": self.name,
                "provider": self.provider,
                "tool_version": self.version,
                "tool_status": self.get_status().value,
                "errors": [{"path": "policy", "message": policy_error}],
                "warnings": [],
            }

        from jsonschema import Draft202012Validator

        schema = self.input_schema or {"type": "object"}
        properties = schema.get("properties", {})
        resolved = dict(inputs)
        for field_name, field_schema in properties.items():
            if field_name not in resolved and "default" in field_schema:
                resolved[field_name] = field_schema["default"]

        declared_inputs = {
            field_name: resolved[field_name]
            for field_name in properties
            if field_name in resolved
        }
        contract_errors = sorted(
            (
                {
                    "path": ".".join(str(part) for part in error.absolute_path),
                    "message": error.message,
                }
                for error in Draft202012Validator(schema).iter_errors(declared_inputs)
            ),
            key=lambda item: (item["path"], item["message"]),
        )

        operation = str(resolved.get("operation") or "")
        reference_values = {
            field_name: resolved.get(field_name)
            for field_name in (
                "image_url",
                "image_path",
                "reference_image_url",
                "reference_image_path",
                "reference_image_urls",
                "reference_image_paths",
                "reference_video_url",
                "reference_video_path",
                "reference_video_urls",
                "reference_audio_urls",
                "image_list",
                "video_list",
                "element_list",
            )
            if resolved.get(field_name)
        }
        binding_contract = dict(self.reference_binding_contract or {})
        supported_binding_modes = list(binding_contract.get("supported_modes") or [])
        requested_binding_modes = sorted(
            {
                str(role.get("binding_mode"))
                for role in (resolved.get("reference_roles") or [])
                if isinstance(role, dict) and role.get("binding_mode")
            }
        )

        if operation == "image_to_video" and not reference_values:
            contract_errors.append(
                {
                    "path": "operation",
                    "message": "image_to_video requires a declared image reference input",
                }
            )
        if operation == "reference_to_video" and not reference_values:
            contract_errors.append(
                {
                    "path": "operation",
                    "message": "reference_to_video requires at least one declared reference input",
                }
            )
        for mode in requested_binding_modes:
            if mode == "internal_only":
                continue
            if mode not in supported_binding_modes:
                contract_errors.append(
                    {
                        "path": "reference_roles",
                        "message": (
                            f"reference binding mode {mode!r} is not declared by {self.name}"
                        ),
                    }
                )
            if mode == "prompt_token" and not binding_contract.get("prompt_token_syntax"):
                contract_errors.append(
                    {
                        "path": "reference_roles",
                        "message": (
                            f"{self.name} does not declare provider prompt-token syntax"
                        ),
                    }
                )

        schema_fingerprint = hashlib.sha256(
            json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        model_selection = next(
            (
                {"field": field_name, "value": resolved[field_name]}
                for field_name in ("model_name", "model", "model_variant")
                if resolved.get(field_name) is not None
            ),
            None,
        )

        live_probe = (
            self.probe_provider_contract(resolved)
            if live
            else {
                "status": "not_requested",
                "verification_scope": [],
                "warnings": [],
                "errors": [],
            }
        )
        errors = list(contract_errors) + [
            {"path": "live_probe", "message": str(message)}
            for message in live_probe.get("errors", [])
        ]
        warnings = [str(message) for message in live_probe.get("warnings", [])]
        tool_status = self.get_status().value
        if tool_status == ToolStatus.UNAVAILABLE.value:
            errors.append(
                {"path": "tool_status", "message": "tool dependencies or credentials are unavailable"}
            )

        live_status = str(live_probe.get("status") or "unverified")
        if errors or live_status == "blocked":
            status = "blocked"
        elif live and live_status not in {"passed"}:
            status = "degraded"
        else:
            status = "passed"

        return {
            "status": status,
            "verification_level": (
                "live_provider_contract" if live_status == "passed" else "declared_tool_contract"
            ),
            "tool": self.name,
            "provider": self.provider,
            "tool_version": self.version,
            "tool_status": tool_status,
            "operation": operation,
            "execution_scope": str(resolved.get("execution_scope") or "sample"),
            "degraded_preflight_approved": bool(
                resolved.get("allow_degraded_preflight", False)
            ),
            "model_selection": model_selection,
            "input_schema_fingerprint": schema_fingerprint,
            "declared_input_fields": sorted(properties),
            "resolved_input_fields": sorted(declared_inputs),
            "reference_binding": {
                "requested_modes": requested_binding_modes,
                "supported_modes": supported_binding_modes,
                "input_fields": list(binding_contract.get("input_fields") or []),
                "prompt_token_syntax": binding_contract.get("prompt_token_syntax"),
            },
            "live_probe": live_probe,
            "errors": errors,
            "warnings": warnings,
            "would_execute": status != "blocked",
        }

    # ---- CLI helper ----

    def run_command(
        self,
        cmd: list[str],
        *,
        timeout: Optional[int] = None,
        cwd: Optional[Path] = None,
    ) -> subprocess.CompletedProcess:
        """Run a subprocess command with standard error handling.

        On Windows, resolves .cmd/.bat wrappers (e.g. npx, npm) via
        shutil.which() so subprocess.run() can find them without shell=True.
        """
        resolved_cmd = list(cmd)
        if platform.system() == "Windows" and resolved_cmd:
            exe = shutil.which(resolved_cmd[0])
            if exe:
                resolved_cmd[0] = exe
        try:
            return subprocess.run(
                resolved_cmd,
                capture_output=True,
                text=True,
                # Force UTF-8 decoding. The default uses the OS locale (cp1252 on
                # Windows), which raises UnicodeDecodeError on a subprocess that
                # emits Unicode/emoji (e.g. Remotion's progress output), killing the
                # reader thread and potentially swallowing the real error text.
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=cwd,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            detail = stderr or stdout or str(exc)
            raise ToolCommandError(
                exc.returncode,
                exc.cmd,
                output=exc.output,
                stderr=exc.stderr,
                detail=detail,
            ) from exc


class ToolCommandError(subprocess.CalledProcessError):
    """CalledProcessError with stderr/stdout surfaced in str(error)."""

    def __init__(
        self,
        returncode: int,
        cmd: list[str],
        *,
        output: Optional[str] = None,
        stderr: Optional[str] = None,
        detail: str = "",
    ) -> None:
        super().__init__(returncode, cmd, output=output, stderr=stderr)
        self.detail = detail

    def __str__(self) -> str:
        base = super().__str__()
        if self.detail:
            return f"{base}\n{self.detail}"
        return base


class DependencyError(Exception):
    """Raised when a tool's dependency is not satisfied."""

    pass
