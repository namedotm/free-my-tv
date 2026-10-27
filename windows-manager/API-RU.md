# Локальный API Xiaomi TV Manager

Сервер слушает только `127.0.0.1` на случайном порту (`START.bat` открывает браузер со ссылкой
`http://127.0.0.1:<порт>/#<токен>`). Наружу — в домашнюю сеть или интернет — API не публикуется.

Защита каждого запроса, кроме статических файлов:
- заголовок `Host` строго `127.0.0.1:<порт>`; `Origin`, если есть, — тот же адрес;
- заголовок `X-Manager-Token` с токеном из ссылки (сравнение постоянного времени);
- JSON-маршруты: `Content-Type: application/json`, тело от 1 байта до 65 536 байт;
- CSP: `default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'none'`.

Ответ ошибки: HTTP 400 `{"error": "понятный текст", "code": "КОД", "details": "технические сведения"}`.
В `details` ссылки и токены вырезаются.

## Статика

`GET /`, `GET /ui.js`, `GET /style.css` — только при точном `Host`.

## Состояние

`GET /status` → `busy`, `message`, `rows` (по устройствам: модель, Android, API, ABI, `free_mb`,
`ram_mb`, `ram_total_mb`, `home`, `interfaces`, `vpn`, `warnings`, `apps` — шаги с `status`
`queued|running|done|error|skipped|manual`), `events` (последние 300), `config`, `source_profile`,
`source_status`, `source_device` (без ключа устройства), `uploads` (файлы и текущая задача APK;
иконки не передаются — см. `/apk icon`).

## POST /action — фоновые действия по выбранным ТВ

Тело: `action`, `ips` (до 3, пустые строки — не выбранные), `all_ips`, `use`, `apps`, `folder`,
`official_youtube`, `source_ip`, `sync_ui`, `media_player`.

| action | Что делает |
|---|---|
| `check` | Только чтение, отчёт `before.json` + `REPORT-RU.txt` |
| `vpn_check` | Какие UID реально в туннеле Android (`dumpsys connectivity`) |
| `measure` | `am start -W` ×3 для выбранных плееров, медиана, сравнение с прошлым замером |
| `install`, `retry` | Каталог: закреплённые источники, SHA-256 и сертификаты |
| `profile` | Профиль ByeByeDPI в `Download` (импорт — на ТВ) |
| `read_source` | Профиль проектора + запоминание проектора по серийному номеру |
| `sync_ui`, `restore_ui`, `network_profile` | UI-профиль Free My TV на приставках |
| `open_incy` | Открыть INCY |
| `diagnose_home` | Владельцы сохранённых Wi-Fi, текущий HOME, путь ADB (Wi-Fi/Ethernet) |
| `existing_setup`, `open_media` | SMB + профиль + передача адреса плееру |
| `play_test` | Открыть самый маленький видеофайл папки в плеере и ждать `PLAYING` в `dumpsys media_session` |

## POST /upload — потоковая загрузка файла (новое)

Заголовки: `X-Manager-Token`, `Content-Type: application/octet-stream`,
`X-File-Name: <имя в URL-кодировке>`, `Content-Length` (1 байт … 1 ГБ). Тело — сам файл.
Файл пишется на диск частями по 1 МБ в `data/uploads/<id>/upload.part`, после полного
получения переименовывается в `original.apk|apks|xapk`. Имя пользователя — только подпись:
путь задаёт менеджер, так что `..\`, `C:\` и зарезервированные имена ничего не дают.
Обрыв связи или отмена → статус `error|cancelled`, частичный файл удаляется. Не более двух
одновременных загрузок; хранится 30 последних файлов. Ответ `{"id": "...", "message": "..."}`.
Анализ запускается сам; результат — в `/status → uploads.items[].analysis`.

## POST /apk (новое)

| mode | Поля | Результат |
|---|---|---|
| `url` | `url` | Скачивание только `https://` с публичных адресов, порт 443, без логина в ссылке; каждый редирект проверяется так же; адреса домашней сети, `localhost`, `.local` запрещены; TLS не отключается |
| `cancel` | `id` | Отмена незавершённой загрузки |
| `delete` | `id` | Удалить файл из менеджера (не с ТВ) |
| `icon` | `id` | `{"icon": "data:image/png;base64,…"}` или `null` |
| `plan` | `id`, `ips` | Фоновая проверка ТВ; строки в `uploads.jobs.rows`, токен в `uploads.jobs.plan` (3 минуты, одноразовый) |
| `install` | `plan` | Фоновая установка по плану с повторной проверкой устройства, версии и файла; журнал в `data/apk-installs/` |

Статусы строки плана: `ready` (новая установка), `update`, `already`, `blocked` (+`reasons`),
`error`. После установки: `done` (+`new_install`), `error` (+`code`), `manual` (ждёт ответа на ТВ),
`skipped`.

Анализ (`analysis`): `kind` (`apk|apks|xapk`), `package`, `label`, `version`, `version_name`,
`min_api`, `target_api`, `abis`, `base_abis`, `sha256`, `certificates`, `schemes`, `debug_cert`,
`test_only`, `leanback`, `parts[]`, `obb_files`, `provenance.kind`
(`catalog|catalog_signer|catalog_mismatch|unknown`), `warnings[]`.

## POST /apps — приложения

`address` + `mode`:
- `scan` → `rows[]` (package, label, installed, system, disabled, protected, `kind`, `kind_text`,
  `advice`, `homemode_state`, `app_bytes`, `data_bytes`, `cache_bytes`), `summary`, `history`;
- `plan` (`operation`: `disable|enable|uninstall_keep_data|restore`, `packages` 1–12) → токен
  на 3 минуты, привязанный к устройству и исходным состояниям;
- `apply` (`plan`) → журнал `changes`, `metrics` до/после;
- `undo` → откат последней операции этого устройства;
- `settings` (`package`) → открыть системную карточку приложения на ТВ;
- `cache_plan` → токен и текущий размер кэша (проектор-источник запрещён);
- `cache_apply` (`plan`) → `pm trim-caches`, свободное место до/после.

## POST /remote (новое)

`mode`: `states` (`ips`) — состояние ADB каждого адреса; `connect`, `disconnect` (`address`);
`key` (`address`, `key` ∈ up, down, left, right, ok, back, home, menu, play, stop, next, prev,
vol_up, vol_down, mute, settings, apps); `screenshot` → PNG как data URL + файл в
`data/screenshots`; `apps` → приложения со значком запуска; `launch` (`package`).
Команды выполняются только по нажатию; фонового агента и автоподключения нет.

## POST /wifi (новое)

`address` + `mode`: `plan` → сохранённые сети (`network_id`, `owner`, `current`, `allowed`,
`why`; SSID — только в ответе для отображения, в отчёты не пишется), токен на 3 минуты;
`forget` (`plan`, `network_id`) → `cmd wifi forget-network` только для записи первоначальной
настройки, которая не является текущим подключением при ADB по Wi-Fi; результат перепроверяется.
Android 9 (API 28) команду не поддерживает — менеджер так и говорит.

## Прочие маршруты (без изменений)

`POST /folder`, `POST /smb`, `POST /restore`, `POST /manual-profile`, `POST /media-info` —
см. [W-INTAKE-RU.md](W-INTAKE-RU.md) и [TRANSFER-RU.md](TRANSFER-RU.md).

## Free My TV ProfileInbox (Android)

`content://io.freemytv.profile`, процесс `:home`, доступ только ADB shell (UID 2000) и самому
приложению: `query /export`, `call export|apply|restore` с Base64 JSON `schema=1`,
`kind=free-my-tv-ui`. Подробно — [TRANSFER-RU.md](TRANSFER-RU.md). Android APK в этом этапе
не менялся.
