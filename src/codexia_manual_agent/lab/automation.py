from __future__ import annotations

import hmac
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from codexia_manual_agent.lab.comparison import (
    FrozenComparisonPolicy,
    SqliteComparisonRegistry,
)
from codexia_manual_agent.lab.errors import (
    EvidenceBindingError,
    InvalidLabRecordError,
    LabIdentityConflictError,
    LabPersistenceError,
    LabPersistenceIntegrityError,
    LabRegistryStateError,
)
from codexia_manual_agent.lab.governed_python import PythonJsonExperimentSpec
from codexia_manual_agent.lab.registry import LabRegistryRecovery, SqliteLabRegistry


AUTOMATION_BUDGET_SCHEMA_VERSION = 1
AUTOMATION_STOP_POLICY_SCHEMA_VERSION = 1
AUTOMATION_PLAN_SCHEMA_VERSION = 1
FROZEN_AUTOMATION_PLAN_SCHEMA_VERSION = 1
MAX_AUTOMATION_STEPS = 4_096
MAX_AUTOMATION_RUNS = 128
MAX_AUTOMATION_JSON_CHARS = 262_144
MAX_TIMESTAMP_CHARS = 64

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidLabRecordError("Automation payload is not canonical JSON") from exc
    if len(encoded) > MAX_AUTOMATION_JSON_CHARS:
        raise InvalidLabRecordError("Automation payload exceeds its byte budget")
    return encoded


def _digest(value: Mapping[str, Any]) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _exact_keys(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidLabRecordError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise InvalidLabRecordError(
            f"{label} keys mismatch; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )
    return value


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidLabRecordError(f"{field_name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidLabRecordError(f"{field_name} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise InvalidLabRecordError(
            f"{field_name} must use lowercase hyphenated UUID form"
        )
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidLabRecordError(f"{field_name} must be lowercase SHA-256 hex")
    return value


def _validate_timestamp(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TIMESTAMP_CHARS
        or "\x00" in value
    ):
        raise InvalidLabRecordError(f"{field_name} must be bounded canonical ISO-8601")
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidLabRecordError(f"{field_name} must be canonical ISO-8601") from exc
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise InvalidLabRecordError(f"{field_name} must be canonical ISO-8601")
    return value


def _canonical_workspace_root(value: str | Path) -> str:
    try:
        resolved = Path(value).expanduser().resolve(strict=True)
    except (TypeError, OSError, RuntimeError) as exc:
        raise InvalidLabRecordError("Automation workspace_root does not resolve") from exc
    if not resolved.is_dir():
        raise InvalidLabRecordError("Automation workspace_root must be a directory")
    return str(resolved)


def _new_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _automation_id(policy_id: str) -> str:
    _validate_uuid(policy_id, "policy_id")
    return str(uuid5(NAMESPACE_URL, f"codexia:m5.1:automation-plan:{policy_id}"))


@dataclass(frozen=True, slots=True)
class AutomationBudget:
    schema_version: int
    max_steps: int
    max_runs: int

    @classmethod
    def create(cls, *, max_steps: int, max_runs: int) -> "AutomationBudget":
        return cls(
            schema_version=AUTOMATION_BUDGET_SCHEMA_VERSION,
            max_steps=max_steps,
            max_runs=max_runs,
        )

    def __post_init__(self) -> None:
        if self.schema_version != AUTOMATION_BUDGET_SCHEMA_VERSION:
            raise InvalidLabRecordError("Unsupported M5.1 automation budget schema")
        if (
            type(self.max_steps) is not int
            or not 1 <= self.max_steps <= MAX_AUTOMATION_STEPS
        ):
            raise InvalidLabRecordError("max_steps must be within the M5.1 budget")
        if (
            type(self.max_runs) is not int
            or not 1 <= self.max_runs <= MAX_AUTOMATION_RUNS
        ):
            raise InvalidLabRecordError("max_runs must be within the M5.1 budget")

    def to_dict(self) -> dict[str, int]:
        return {
            "schema_version": self.schema_version,
            "max_steps": self.max_steps,
            "max_runs": self.max_runs,
        }


@dataclass(frozen=True, slots=True)
class AutomationStopPolicy:
    schema_version: int
    pause_on_authorization_required: bool
    stop_on_error: bool
    stop_on_budget_exhaustion: bool
    stop_on_conclusion: bool

    @classmethod
    def strict_v1(cls) -> "AutomationStopPolicy":
        return cls(
            schema_version=AUTOMATION_STOP_POLICY_SCHEMA_VERSION,
            pause_on_authorization_required=True,
            stop_on_error=True,
            stop_on_budget_exhaustion=True,
            stop_on_conclusion=True,
        )

    def __post_init__(self) -> None:
        if self.schema_version != AUTOMATION_STOP_POLICY_SCHEMA_VERSION:
            raise InvalidLabRecordError("Unsupported M5.1 automation stop-policy schema")
        for field_name in (
            "pause_on_authorization_required",
            "stop_on_error",
            "stop_on_budget_exhaustion",
            "stop_on_conclusion",
        ):
            value = getattr(self, field_name)
            if type(value) is not bool:
                raise InvalidLabRecordError(f"{field_name} must be boolean")
            if not value:
                raise InvalidLabRecordError(f"M5.1 v1 requires {field_name}=true")

    def to_dict(self) -> dict[str, bool | int]:
        return {
            "schema_version": self.schema_version,
            "pause_on_authorization_required": self.pause_on_authorization_required,
            "stop_on_error": self.stop_on_error,
            "stop_on_budget_exhaustion": self.stop_on_budget_exhaustion,
            "stop_on_conclusion": self.stop_on_conclusion,
        }


@dataclass(frozen=True, slots=True)
class AutomationPlan:
    schema_version: int
    automation_id: str
    created_at: str
    workspace_root: str
    policy_id: str
    policy_digest: str
    policy_freeze_digest: str
    baseline_experiment_id: str
    baseline_manifest_digest: str
    candidate_experiment_id: str
    candidate_manifest_digest: str
    required_runs: int
    budget: AutomationBudget
    stop_policy: AutomationStopPolicy
    plan_digest: str

    @classmethod
    def create(
        cls,
        *,
        frozen_policy: FrozenComparisonPolicy,
        workspace_root: str | Path,
        budget: AutomationBudget,
        stop_policy: AutomationStopPolicy | None = None,
        created_at: str | None = None,
    ) -> "AutomationPlan":
        if not isinstance(frozen_policy, FrozenComparisonPolicy):
            raise TypeError("frozen_policy must be a FrozenComparisonPolicy")
        if not isinstance(budget, AutomationBudget):
            raise TypeError("budget must be an AutomationBudget")
        stop_policy = stop_policy or AutomationStopPolicy.strict_v1()
        if not isinstance(stop_policy, AutomationStopPolicy):
            raise TypeError("stop_policy must be an AutomationStopPolicy")
        canonical_workspace = _canonical_workspace_root(workspace_root)
        policy = frozen_policy.policy
        required_runs = len(policy.seeds) * 2
        if budget.max_runs > required_runs:
            raise InvalidLabRecordError(
                "Automation max_runs cannot exceed the exact frozen comparison run set"
            )
        automation_id = _automation_id(policy.policy_id)
        created_at = created_at or _new_timestamp()
        base = {
            "schema_version": AUTOMATION_PLAN_SCHEMA_VERSION,
            "automation_id": automation_id,
            "created_at": created_at,
            "workspace_root": canonical_workspace,
            "policy_id": policy.policy_id,
            "policy_digest": policy.policy_digest,
            "policy_freeze_digest": frozen_policy.freeze_digest,
            "baseline_experiment_id": policy.baseline_experiment_id,
            "baseline_manifest_digest": policy.baseline_manifest_digest,
            "candidate_experiment_id": policy.candidate_experiment_id,
            "candidate_manifest_digest": policy.candidate_manifest_digest,
            "required_runs": required_runs,
            "budget": budget.to_dict(),
            "stop_policy": stop_policy.to_dict(),
        }
        return cls(
            schema_version=AUTOMATION_PLAN_SCHEMA_VERSION,
            automation_id=automation_id,
            created_at=created_at,
            workspace_root=canonical_workspace,
            policy_id=policy.policy_id,
            policy_digest=policy.policy_digest,
            policy_freeze_digest=frozen_policy.freeze_digest,
            baseline_experiment_id=policy.baseline_experiment_id,
            baseline_manifest_digest=policy.baseline_manifest_digest,
            candidate_experiment_id=policy.candidate_experiment_id,
            candidate_manifest_digest=policy.candidate_manifest_digest,
            required_runs=required_runs,
            budget=budget,
            stop_policy=stop_policy,
            plan_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != AUTOMATION_PLAN_SCHEMA_VERSION:
            raise InvalidLabRecordError("Unsupported M5.1 automation plan schema")
        _validate_uuid(self.automation_id, "automation_id")
        _validate_timestamp(self.created_at, "created_at")
        object.__setattr__(
            self,
            "workspace_root",
            _canonical_workspace_root(self.workspace_root),
        )
        _validate_uuid(self.policy_id, "policy_id")
        _validate_digest(self.policy_digest, "policy_digest")
        _validate_digest(self.policy_freeze_digest, "policy_freeze_digest")
        _validate_uuid(self.baseline_experiment_id, "baseline_experiment_id")
        _validate_digest(self.baseline_manifest_digest, "baseline_manifest_digest")
        _validate_uuid(self.candidate_experiment_id, "candidate_experiment_id")
        _validate_digest(self.candidate_manifest_digest, "candidate_manifest_digest")
        if self.baseline_experiment_id == self.candidate_experiment_id:
            raise EvidenceBindingError("Automation comparison arms must remain distinct")
        if self.automation_id != _automation_id(self.policy_id):
            raise InvalidLabRecordError(
                "automation_id is not deterministic for the frozen policy"
            )
        if (
            type(self.required_runs) is not int
            or not 2 <= self.required_runs <= MAX_AUTOMATION_RUNS
        ):
            raise InvalidLabRecordError("required_runs is outside the M5.1 bound")
        if not isinstance(self.budget, AutomationBudget):
            raise TypeError("budget must be an AutomationBudget")
        if not isinstance(self.stop_policy, AutomationStopPolicy):
            raise TypeError("stop_policy must be an AutomationStopPolicy")
        if self.budget.max_runs > self.required_runs:
            raise InvalidLabRecordError(
                "Automation max_runs cannot exceed required_runs"
            )
        _validate_digest(self.plan_digest, "plan_digest")
        if not hmac.compare_digest(_digest(self._payload()), self.plan_digest):
            raise InvalidLabRecordError(
                "Automation plan digest does not match exact payload"
            )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "automation_id": self.automation_id,
            "created_at": self.created_at,
            "workspace_root": self.workspace_root,
            "policy_id": self.policy_id,
            "policy_digest": self.policy_digest,
            "policy_freeze_digest": self.policy_freeze_digest,
            "baseline_experiment_id": self.baseline_experiment_id,
            "baseline_manifest_digest": self.baseline_manifest_digest,
            "candidate_experiment_id": self.candidate_experiment_id,
            "candidate_manifest_digest": self.candidate_manifest_digest,
            "required_runs": self.required_runs,
            "budget": self.budget.to_dict(),
            "stop_policy": self.stop_policy.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "plan_digest": self.plan_digest}


@dataclass(frozen=True, slots=True)
class FrozenAutomationPlan:
    schema_version: int
    plan: AutomationPlan
    frozen_at: str
    baseline_event_id: str
    baseline_event_sequence: int
    baseline_event_digest: str
    candidate_event_id: str
    candidate_event_sequence: int
    candidate_event_digest: str
    freeze_digest: str

    @classmethod
    def create(
        cls,
        *,
        plan: AutomationPlan,
        baseline_event_id: str,
        baseline_event_sequence: int,
        baseline_event_digest: str,
        candidate_event_id: str,
        candidate_event_sequence: int,
        candidate_event_digest: str,
        frozen_at: str | None = None,
    ) -> "FrozenAutomationPlan":
        if not isinstance(plan, AutomationPlan):
            raise TypeError("plan must be an AutomationPlan")
        frozen_at = frozen_at or _new_timestamp()
        base = {
            "schema_version": FROZEN_AUTOMATION_PLAN_SCHEMA_VERSION,
            "plan": plan.to_dict(),
            "frozen_at": frozen_at,
            "baseline_event_id": baseline_event_id,
            "baseline_event_sequence": baseline_event_sequence,
            "baseline_event_digest": baseline_event_digest,
            "candidate_event_id": candidate_event_id,
            "candidate_event_sequence": candidate_event_sequence,
            "candidate_event_digest": candidate_event_digest,
        }
        return cls(
            schema_version=FROZEN_AUTOMATION_PLAN_SCHEMA_VERSION,
            plan=plan,
            frozen_at=frozen_at,
            baseline_event_id=baseline_event_id,
            baseline_event_sequence=baseline_event_sequence,
            baseline_event_digest=baseline_event_digest,
            candidate_event_id=candidate_event_id,
            candidate_event_sequence=candidate_event_sequence,
            candidate_event_digest=candidate_event_digest,
            freeze_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != FROZEN_AUTOMATION_PLAN_SCHEMA_VERSION:
            raise InvalidLabRecordError("Unsupported M5.1 frozen automation schema")
        if not isinstance(self.plan, AutomationPlan):
            raise TypeError("plan must be an AutomationPlan")
        _validate_timestamp(self.frozen_at, "frozen_at")
        for field_name in ("baseline_event_id", "candidate_event_id"):
            _validate_uuid(getattr(self, field_name), field_name)
        for field_name in ("baseline_event_sequence", "candidate_event_sequence"):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise InvalidLabRecordError(f"{field_name} must be non-negative")
        for field_name in (
            "baseline_event_digest",
            "candidate_event_digest",
            "freeze_digest",
        ):
            _validate_digest(getattr(self, field_name), field_name)
        if not hmac.compare_digest(_digest(self._payload()), self.freeze_digest):
            raise InvalidLabRecordError(
                "Frozen automation digest does not match exact payload"
            )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan": self.plan.to_dict(),
            "frozen_at": self.frozen_at,
            "baseline_event_id": self.baseline_event_id,
            "baseline_event_sequence": self.baseline_event_sequence,
            "baseline_event_digest": self.baseline_event_digest,
            "candidate_event_id": self.candidate_event_id,
            "candidate_event_sequence": self.candidate_event_sequence,
            "candidate_event_digest": self.candidate_event_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "freeze_digest": self.freeze_digest}


def automation_budget_from_dict(value: Any) -> AutomationBudget:
    data = _exact_keys(
        value,
        {"schema_version", "max_steps", "max_runs"},
        "automation budget",
    )
    return AutomationBudget(
        schema_version=data["schema_version"],
        max_steps=data["max_steps"],
        max_runs=data["max_runs"],
    )


def automation_stop_policy_from_dict(value: Any) -> AutomationStopPolicy:
    data = _exact_keys(
        value,
        {
            "schema_version",
            "pause_on_authorization_required",
            "stop_on_error",
            "stop_on_budget_exhaustion",
            "stop_on_conclusion",
        },
        "automation stop policy",
    )
    return AutomationStopPolicy(
        schema_version=data["schema_version"],
        pause_on_authorization_required=data["pause_on_authorization_required"],
        stop_on_error=data["stop_on_error"],
        stop_on_budget_exhaustion=data["stop_on_budget_exhaustion"],
        stop_on_conclusion=data["stop_on_conclusion"],
    )


def automation_plan_from_dict(value: Any) -> AutomationPlan:
    data = _exact_keys(
        value,
        {
            "schema_version",
            "automation_id",
            "created_at",
            "workspace_root",
            "policy_id",
            "policy_digest",
            "policy_freeze_digest",
            "baseline_experiment_id",
            "baseline_manifest_digest",
            "candidate_experiment_id",
            "candidate_manifest_digest",
            "required_runs",
            "budget",
            "stop_policy",
            "plan_digest",
        },
        "automation plan",
    )
    return AutomationPlan(
        schema_version=data["schema_version"],
        automation_id=data["automation_id"],
        created_at=data["created_at"],
        workspace_root=data["workspace_root"],
        policy_id=data["policy_id"],
        policy_digest=data["policy_digest"],
        policy_freeze_digest=data["policy_freeze_digest"],
        baseline_experiment_id=data["baseline_experiment_id"],
        baseline_manifest_digest=data["baseline_manifest_digest"],
        candidate_experiment_id=data["candidate_experiment_id"],
        candidate_manifest_digest=data["candidate_manifest_digest"],
        required_runs=data["required_runs"],
        budget=automation_budget_from_dict(data["budget"]),
        stop_policy=automation_stop_policy_from_dict(data["stop_policy"]),
        plan_digest=data["plan_digest"],
    )


def frozen_automation_plan_from_dict(value: Any) -> FrozenAutomationPlan:
    data = _exact_keys(
        value,
        {
            "schema_version",
            "plan",
            "frozen_at",
            "baseline_event_id",
            "baseline_event_sequence",
            "baseline_event_digest",
            "candidate_event_id",
            "candidate_event_sequence",
            "candidate_event_digest",
            "freeze_digest",
        },
        "frozen automation plan",
    )
    return FrozenAutomationPlan(
        schema_version=data["schema_version"],
        plan=automation_plan_from_dict(data["plan"]),
        frozen_at=data["frozen_at"],
        baseline_event_id=data["baseline_event_id"],
        baseline_event_sequence=data["baseline_event_sequence"],
        baseline_event_digest=data["baseline_event_digest"],
        candidate_event_id=data["candidate_event_id"],
        candidate_event_sequence=data["candidate_event_sequence"],
        candidate_event_digest=data["candidate_event_digest"],
        freeze_digest=data["freeze_digest"],
    )


class SqliteAutomationPlanRegistry:
    """Freeze M5.1 automation scope/budgets before any automated M4 run work."""

    def __init__(
        self,
        comparisons: SqliteComparisonRegistry,
        lab_registry: SqliteLabRegistry,
    ) -> None:
        if not isinstance(comparisons, SqliteComparisonRegistry):
            raise TypeError("comparisons must be a SqliteComparisonRegistry")
        if not isinstance(lab_registry, SqliteLabRegistry):
            raise TypeError("lab_registry must be a SqliteLabRegistry")
        if comparisons.database_path != lab_registry.database_path:
            raise ValueError("Automation registries must share one SQLite trust domain")
        self._comparisons = comparisons
        self._lab = lab_registry
        self._database_path = lab_registry.database_path
        self._initialize()

    @property
    def database_path(self) -> Path:
        return self._database_path

    @contextmanager
    def _connect(self):
        try:
            with closing(
                sqlite3.connect(
                    self._database_path,
                    timeout=30.0,
                    isolation_level=None,
                )
            ) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA busy_timeout = 30000")
                yield connection
        except sqlite3.Error as exc:
            raise LabPersistenceError("SQLite automation-plan operation failed") from exc

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS lab_automation_plan_freezes (
                    automation_id TEXT PRIMARY KEY,
                    policy_id TEXT NOT NULL UNIQUE,
                    plan_digest TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    freeze_digest TEXT NOT NULL,
                    FOREIGN KEY (policy_id)
                        REFERENCES lab_comparison_policy_freezes(policy_id)
                )
                """
            )

    def register_plan(self, plan: AutomationPlan) -> FrozenAutomationPlan:
        if not isinstance(plan, AutomationPlan):
            raise TypeError("plan must be an AutomationPlan")
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM lab_automation_plan_freezes WHERE automation_id = ?",
                    (plan.automation_id,),
                ).fetchone()
                if existing is not None:
                    connection.execute("COMMIT")
                    recovered = self.recover(plan.automation_id)
                    if recovered.plan != plan:
                        raise LabIdentityConflictError(
                            "Automation id is already bound to another exact plan"
                        )
                    return recovered
                policy_owner = connection.execute(
                    "SELECT automation_id FROM lab_automation_plan_freezes WHERE policy_id = ?",
                    (plan.policy_id,),
                ).fetchone()
                if policy_owner is not None:
                    raise LabIdentityConflictError(
                        "Frozen policy already has an M5.1 automation plan"
                    )
                digest_owner = connection.execute(
                    "SELECT automation_id FROM lab_automation_plan_freezes WHERE plan_digest = ?",
                    (plan.plan_digest,),
                ).fetchone()
                if digest_owner is not None:
                    raise LabIdentityConflictError(
                        "Exact automation plan digest is already frozen under another id"
                    )

                frozen_policy = self._comparisons.recover_policy(plan.policy_id)
                self._validate_plan_policy(plan, frozen_policy)
                baseline = self._lab.recover_experiment(plan.baseline_experiment_id)
                candidate = self._lab.recover_experiment(plan.candidate_experiment_id)
                self._validate_lab_dependencies(
                    plan,
                    frozen_policy,
                    baseline,
                    candidate,
                    require_preautomation=True,
                )
                baseline_event = baseline.events[-1]
                candidate_event = candidate.events[-1]
                frozen = FrozenAutomationPlan.create(
                    plan=plan,
                    baseline_event_id=baseline_event.event_id,
                    baseline_event_sequence=baseline_event.sequence,
                    baseline_event_digest=baseline_event.event_digest,
                    candidate_event_id=candidate_event.event_id,
                    candidate_event_sequence=candidate_event.sequence,
                    candidate_event_digest=candidate_event.event_digest,
                )
                raw = _canonical_json(frozen.to_dict())
                connection.execute(
                    """
                    INSERT INTO lab_automation_plan_freezes(
                        automation_id, policy_id, plan_digest, payload_json, freeze_digest
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        plan.automation_id,
                        plan.policy_id,
                        plan.plan_digest,
                        raw,
                        frozen.freeze_digest,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return self.recover(plan.automation_id)

    def recover(self, automation_id: str) -> FrozenAutomationPlan:
        _validate_uuid(automation_id, "automation_id")
        with self._connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM lab_automation_plan_freezes WHERE automation_id = ?",
                (automation_id,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise InvalidLabRecordError("Unknown frozen automation plan")
            raw = row["payload_json"]
            if not isinstance(raw, str) or len(raw) > MAX_AUTOMATION_JSON_CHARS:
                connection.execute("ROLLBACK")
                raise LabPersistenceIntegrityError(
                    "Persisted automation plan JSON is invalid"
                )
            try:
                value = json.loads(raw)
                frozen = frozen_automation_plan_from_dict(value)
                canonical = _canonical_json(frozen.to_dict())
            except (
                InvalidLabRecordError,
                EvidenceBindingError,
                TypeError,
                ValueError,
                RecursionError,
                json.JSONDecodeError,
            ) as exc:
                connection.execute("ROLLBACK")
                raise LabPersistenceIntegrityError(
                    "Persisted automation plan failed validation"
                ) from exc
            if canonical != raw:
                connection.execute("ROLLBACK")
                raise LabPersistenceIntegrityError(
                    "Persisted automation plan JSON is not canonical"
                )
            if (
                row["automation_id"] != frozen.plan.automation_id
                or row["policy_id"] != frozen.plan.policy_id
                or row["plan_digest"] != frozen.plan.plan_digest
                or row["freeze_digest"] != frozen.freeze_digest
            ):
                connection.execute("ROLLBACK")
                raise LabPersistenceIntegrityError(
                    "Persisted automation-plan indexes disagree with payload"
                )
            connection.execute("COMMIT")

        try:
            frozen_policy = self._comparisons.recover_policy(frozen.plan.policy_id)
            self._validate_plan_policy(frozen.plan, frozen_policy)
            baseline = self._lab.recover_experiment(frozen.plan.baseline_experiment_id)
            candidate = self._lab.recover_experiment(frozen.plan.candidate_experiment_id)
            self._validate_lab_dependencies(
                frozen.plan,
                frozen_policy,
                baseline,
                candidate,
                require_preautomation=False,
            )
            self._validate_freeze_anchor(frozen, baseline, candidate)
        except (
            InvalidLabRecordError,
            EvidenceBindingError,
            LabRegistryStateError,
        ) as exc:
            raise LabPersistenceIntegrityError(
                "Frozen automation plan no longer matches durable M4 state"
            ) from exc
        return frozen

    @staticmethod
    def _validate_plan_policy(
        plan: AutomationPlan,
        frozen_policy: FrozenComparisonPolicy,
    ) -> None:
        policy = frozen_policy.policy
        if (
            plan.policy_id != policy.policy_id
            or not hmac.compare_digest(plan.policy_digest, policy.policy_digest)
            or not hmac.compare_digest(
                plan.policy_freeze_digest,
                frozen_policy.freeze_digest,
            )
            or plan.baseline_experiment_id != policy.baseline_experiment_id
            or not hmac.compare_digest(
                plan.baseline_manifest_digest,
                policy.baseline_manifest_digest,
            )
            or plan.candidate_experiment_id != policy.candidate_experiment_id
            or not hmac.compare_digest(
                plan.candidate_manifest_digest,
                policy.candidate_manifest_digest,
            )
            or plan.required_runs != len(policy.seeds) * 2
        ):
            raise EvidenceBindingError(
                "Automation plan does not bind the exact frozen comparison policy"
            )
        if plan.budget.max_runs > plan.required_runs:
            raise InvalidLabRecordError(
                "Automation run budget exceeds exact frozen comparison scope"
            )

    @staticmethod
    def _validate_lab_dependencies(
        plan: AutomationPlan,
        frozen_policy: FrozenComparisonPolicy,
        baseline: LabRegistryRecovery,
        candidate: LabRegistryRecovery,
        *,
        require_preautomation: bool,
    ) -> None:
        _canonical_workspace_root(plan.workspace_root)
        for label, recovery, experiment_id, manifest_digest in (
            (
                "baseline",
                baseline,
                plan.baseline_experiment_id,
                plan.baseline_manifest_digest,
            ),
            (
                "candidate",
                candidate,
                plan.candidate_experiment_id,
                plan.candidate_manifest_digest,
            ),
        ):
            if recovery.experiment_id != experiment_id:
                raise EvidenceBindingError(
                    f"Automation {label} recovery resolved another experiment"
                )
            if not hmac.compare_digest(
                recovery.manifest.manifest_digest,
                manifest_digest,
            ):
                raise EvidenceBindingError(
                    f"Automation {label} does not match exact manifest"
                )
            PythonJsonExperimentSpec.from_manifest(recovery.manifest)
            if require_preautomation:
                if recovery.experiment_sealed:
                    raise LabRegistryStateError(
                        f"Cannot freeze automation after {label} experiment is sealed"
                    )
                if recovery.runs:
                    raise LabRegistryStateError(
                        f"Cannot freeze automation after {label} has a registered run"
                    )

        policy = frozen_policy.policy
        if (
            baseline.hypothesis.hypothesis_id != policy.hypothesis_id
            or candidate.hypothesis.hypothesis_id != policy.hypothesis_id
            or not hmac.compare_digest(
                baseline.hypothesis.hypothesis_digest,
                policy.hypothesis_digest,
            )
            or not hmac.compare_digest(
                candidate.hypothesis.hypothesis_digest,
                policy.hypothesis_digest,
            )
        ):
            raise EvidenceBindingError(
                "Automation arms no longer bind the exact policy hypothesis"
            )

        if require_preautomation:
            if (
                baseline.events[-1].event_id != frozen_policy.baseline_event_id
                or baseline.events[-1].sequence
                != frozen_policy.baseline_event_sequence
                or not hmac.compare_digest(
                    baseline.events[-1].event_digest,
                    frozen_policy.baseline_event_digest,
                )
                or candidate.events[-1].event_id != frozen_policy.candidate_event_id
                or candidate.events[-1].sequence
                != frozen_policy.candidate_event_sequence
                or not hmac.compare_digest(
                    candidate.events[-1].event_digest,
                    frozen_policy.candidate_event_digest,
                )
            ):
                raise LabRegistryStateError(
                    "M5.1 automation must freeze before either arm advances beyond the M4.4 policy freeze"
                )

    @staticmethod
    def _validate_freeze_anchor(
        frozen: FrozenAutomationPlan,
        baseline: LabRegistryRecovery,
        candidate: LabRegistryRecovery,
    ) -> None:
        for label, recovery, event_id, sequence, digest in (
            (
                "baseline",
                baseline,
                frozen.baseline_event_id,
                frozen.baseline_event_sequence,
                frozen.baseline_event_digest,
            ),
            (
                "candidate",
                candidate,
                frozen.candidate_event_id,
                frozen.candidate_event_sequence,
                frozen.candidate_event_digest,
            ),
        ):
            if sequence >= len(recovery.events):
                raise EvidenceBindingError(
                    f"Automation {label} freeze anchor is outside durable chronology"
                )
            event = recovery.events[sequence]
            if (
                event.event_id != event_id
                or event.sequence != sequence
                or not hmac.compare_digest(event.event_digest, digest)
            ):
                raise EvidenceBindingError(
                    f"Automation {label} freeze anchor no longer matches durable chronology"
                )
