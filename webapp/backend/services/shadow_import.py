"""Read-only shadow import from an explicitly mapped ai-tool golden snapshot."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from repositories.portable_store import PortableDomainStore


class ShadowImportError(ValueError):
    pass


@dataclass(frozen=True)
class LegacyBinding:
    """Explicit non-secret mapping; values are never inferred from runtime state."""

    host_id: str
    profile_id: str
    account_ref: str
    binding_id: str
    binding_generation: int = 1

    def validate(self) -> None:
        for label, value in (("host_id", self.host_id), ("profile_id", self.profile_id),
                             ("account_ref", self.account_ref), ("binding_id", self.binding_id)):
            if not isinstance(value, str) or not value.strip():
                raise ShadowImportError(f"explicit {label} is required")
        if not isinstance(self.binding_generation, int) or self.binding_generation < 1:
            raise ShadowImportError("binding_generation must be positive")
        lowered = self.account_ref.lower()
        if any(word in lowered for word in ("token", "cookie", "password", "secret", "session")):
            raise ShadowImportError("account_ref must be a non-secret reference")


@dataclass(frozen=True)
class GoldenSnapshot:
    clients: tuple[Mapping[str, Any], ...] = ()
    schedules: tuple[Mapping[str, Any], ...] = ()
    policies: tuple[Mapping[str, Any], ...] = ()
    cycle_stopped: bool = False
    observed_at: str = ""


class FileSystemGoldenSource:
    """Allowlisted JSON/text reader; it never writes to the legacy root."""

    def __init__(self, root: Path | str, client_database="client_database.json",
                 master_data="clients_master.json", cycle_flag="cycle_stopped.flag"):
        self.root = Path(root).resolve()
        self.client_database = client_database
        self.master_data = master_data
        self.cycle_flag = cycle_flag
        for relative in (client_database, master_data, cycle_flag):
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                raise ShadowImportError("legacy source path must stay within the explicit root")

    def _path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if self.root not in path.parents:
            raise ShadowImportError("legacy source escaped root")
        return path

    def _read_json(self, relative: str) -> Mapping[str, Any]:
        path = self._path(relative)
        if not path.is_file():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ShadowImportError(f"{relative} must contain an object")
        return value

    def snapshot(self) -> GoldenSnapshot:
        database = self._read_json(self.client_database)
        master = self._read_json(self.master_data)
        clients = database.get("clients") or master.get("clients") or []
        schedules = database.get("schedules") or database.get("schedule") or master.get("schedule") or []
        policies = database.get("policies") or master.get("policies") or []
        if not isinstance(clients, list) or not isinstance(schedules, list) or not isinstance(policies, list):
            raise ShadowImportError("legacy domain collections must be arrays")
        flag_path = self._path(self.cycle_flag)
        flag = flag_path.read_text(encoding="utf-8").strip().lower() if flag_path.is_file() else ""
        return GoldenSnapshot(tuple(clients), tuple(schedules), tuple(policies),
                             flag in {"1", "true", "yes", "stop", "stopped"})


def _value(item: Mapping[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return default


def import_shadow(store: PortableDomainStore, snapshot: GoldenSnapshot,
                  binding: LegacyBinding, now: str) -> dict[str, int | str]:
    """Import a normalized snapshot without touching legacy files or runtime state."""
    binding.validate()
    if not now.strip():
        raise ShadowImportError("now is required")
    store.add_host(binding.host_id, binding.host_id, "legacy-explicit-binding", now)
    store.add_profile(binding.profile_id, binding.profile_id, binding.account_ref, "VERIFIED", now)
    store.bind_profile(binding.binding_id, binding.host_id, binding.profile_id,
                       binding.account_ref, binding.binding_generation, "ACTIVE", now)

    for position, client in enumerate(snapshot.clients):
        client_id = str(_value(client, "client", "client_id", "id", default="")).strip()
        if not client_id:
            raise ShadowImportError("client without explicit client id")
        store.upsert_client(
            binding.profile_id, client_id, str(_value(client, "name", "display_name", default=client_id)),
            str(_value(client, "group", "group_name", default="none")),
            str(_value(client, "status", default="unknown")), "legacy-shadow-import",
        )
    for position, schedule in enumerate(snapshot.schedules):
        schedule_id = str(_value(schedule, "schedule_id", "id", default=f"schedule-{position}"))
        store.upsert_schedule(
            binding.profile_id, schedule_id,
            str(_value(schedule, "group", "group_name", default="none")),
            str(_value(schedule, "open", "open_time", default="")),
            str(_value(schedule, "close", "close_time", default="")),
            bool(_value(schedule, "enabled", default=True)),
        )
    for position, policy in enumerate(snapshot.policies):
        key = str(_value(policy, "key", "policy_key", default=f"policy-{position}"))
        store.upsert_policy(binding.profile_id, str(_value(policy, "policy_id", "id", default=key)),
                            key, str(_value(policy, "value", "policy_value", default="")))
    if snapshot.cycle_stopped:
        store.upsert_cycle_stopped(binding.profile_id, "legacy-cycle-stopped", "REQUESTED", now,
                                   "legacy:cycle_stopped.flag", "shadow import of explicit profile binding")
    store.add_observation(binding.profile_id, f"shadow-{now}", now, "legacy-shadow-import",
                          "import_source", "ai-tool-golden")
    store.add_audit_event(binding.profile_id, f"shadow-{now}", now, "shadow_import",
                          "importer", "read-only legacy shadow import", "legacy-shadow-import")
    return {"profile_id": binding.profile_id, "clients": len(snapshot.clients),
            "schedules": len(snapshot.schedules), "policies": len(snapshot.policies),
            "cycle_stopped": int(snapshot.cycle_stopped)}