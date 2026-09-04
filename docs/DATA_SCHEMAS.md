# Data Schemas (ảnh chụp hình dạng — giá trị thật nằm ở ai tool)

> ⚠️ Không commit dữ liệu thật/token vào repo này. Dưới đây là hình dạng.

## clients_master.json

```json
{
  "clients": [
    {"client": "client_14", "name": "TenHienThi", "remote_name": "...",
     "status": "running|offline", "group": "fixed|HAMI|MNHI|MIE|NA|none",
     "selected": true}
  ],
  "meta": {"extracted_at": "...", "total_remote_clients": 26},
  "schedule": [{"group": "HAMI", "time": "04:00", "close": "08:00"}]
}
```

## client_database.json

```json
{
  "lastUpdated": "2026-09-04",
  "schedule": [{"time": "04:00", "group": "HAMI",
    "open": ["HANMI", "..."], "close": ["NA", "..."],
    "closeAt": "08:00", "closeGroup": "NA", "closeTime": "04:00"}],
  "clients": [{"idx": 0, "client": "client_14", "name": "...",
    "status": "running", "group": "fixed", "selected": true}]
}
```

## cache/settings.json (⚠️ KHÔNG copy giá trị thật)

```json
{
  "telegram_bot_token": "<BOT_TOKEN>",
  "telegram_chat_id": "<CHAT_ID>",
  "default_browser": "default",
  "cloudflared_path": "",
  "tunnel_port": 8080,
  "auto_restart_tunnel": true,
  "auto_telegram": true,
  "auto_open_browser": true
}
```

## cache/cycle_state.json

```json
{"today": "2026-09-04", "done": {"04:00|HAMI": "04:01:29"}}
```

## ai_fix request / done

- Pending: `ai_fix_<cycle|web|userimport>_<YYYYMMDD_HHMMSS>.json`
  `{time, project: "ai tool", kind, command, by}` + userimport có `text`.
- Claim: đổi tên thành `.processing.json`; xong → `.done.json` kèm
  `result {finished_at, summary, model?, files_changed[]}`; hỏng → `.failed.json`.

## cache/manual_override.json

```json
{"client_31": {"until": "<iso>", "detected_at": "<iso>",
  "from": "offline", "to": "running"}}
```
Thao tác tay trên web remote trong 15 phút — bảng Remote Live hiện banner.
