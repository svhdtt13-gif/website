# Phase 3 Golden Delta Audit

Audit date: 2026-09-06

## Method

The current local `ai tool` was queried through its authenticated HTTP API with
read-only `GET` requests. No POST, DELETE, worker restart, direct filesystem
read, or production write was used. Payload values and settings secrets were
excluded from the audit record.

The importer branch was then run against the same live HTTP source. It created
only a temporary candidate SQLite database.

## Current HTTP Surface

All 11 required sources returned HTTP `200`:

| Source | Observed shape and types |
|---|---|
| `api/master` | object with `meta`, `clients`, `schedule`; 26 clients, 4 schedule entries; client fields `client:string`, `name:string`, `remote_name:string`, `group:string`, `status:string`, `selected:bool`; schedule fields `group/time/close:string` |
| `client_database.json` | object with `lastUpdated:string`, `clients`, `schedule`; 26 clients, 4 schedule entries; client fields `idx:number`, `client/name/status/group:string`, `selected:bool` |
| `api/settings` | raw upstream object has 4 public fields plus 4 secret/path fields; values omitted; importer receives only the shared 4-field redacted service projection |
| `api/cycle/status` | object with `state` and `manual_overrides`; `state.today:string`, `state.done:object<string,string>`; manual overrides are an array |
| `cache/activity_history.jsonl` | authenticated text stream; raw bytes preserved |
| `cache/change_log.jsonl` | authenticated text stream; raw bytes preserved |
| `cache/action.log` | authenticated text stream; raw bytes preserved |
| `api/ai_fix/status` | object with `models`, `watcher`, `pending`, `recent_done`, `recent_failed`; status items expose `file`, `kind`, `time`, `summary`, `model`, `answer` as strings; completed filenames use `.done.json` and `.failed.json` |
| `api/cycle/backup` | object with `backups`; backup items include `created_at`, `files`, `label`, `mtime`, `name`, `script_files`, `size`; nested file entries include `path`, `sha256`, `size` |
| `api/status` | `clients:number`, `lastUpdated:string`, `time:string` |
| `api/sync_status` | `continuous_running:bool`, `continuous_pid:number` in the observed running state, `interval_sec:number`, `status_interval_sec:number`, plus string metadata fields |

## Delta Applied

The original Slice 1 fixture did not cover the current response shape. The
branch now accounts for:

- `api/master.meta` while preserving the normalized clients/schedule projection;
- nested backup `files` and `script_files` metadata;
- `.done.json` and `.failed.json` AI-fix filenames;
- strict current cycle state, ledger, and manual-override fields;
- strict current status/sync types;
- the upstream settings response containing secrets, which is rejected by the
  importer unless it has already passed through the four-field redacted service.

## Live Import Result

Using `AiToolHttpSource` against the current authenticated HTTP surface:

- stable two-pass snapshot: **passed**, 11 sources;
- temporary candidate import: **verified**;
- shadow checks: **17 passed**;
- no upstream write occurred.

This audit does not approve production read cutover, dual writes, route changes,
worker ownership changes, or deferred runtime-control actions.
