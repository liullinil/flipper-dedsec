# Служебные скрипты для Flipper Zero по USB

Работают через встроенную командную строку Flipper (виртуальный COM-порт, на этой машине COM5).
Нужен Python 3.9+ и `pyserial`.

| Скрипт | Назначение |
|---|---|
| `flipper_lib.py` | Библиотека: `list`, `walk`, `stat`, `remove`, `rmtree`, `mkdir`, `md5`, `receive_file`, `send_file` |
| `inventory.py COM5 out.txt` | Полная опись `apps`, `apps_data`, `apps_assets`, `apps_manifests`, `update` |
| `backup.py COM5 dest_dir` | Копия личных записей и небольших папок `apps_data` на ПК |
| `install_update.py COM5 pkg_dir [--install]` | Загрузка пакета обновления в `/ext/update` с проверкой md5 и запуск установки |
| `wait_port.py [sec]` | Ожидание перезагрузки и вывод версии прошивки |
| `cleanup.py COM5 [--dry]` | Удаление всего, что не принадлежит прошивке по `/ext/Manifest` |
| `build_anims.py` / `upload_anims.py` | Сборка и заливка WD-анимаций рабочего стола (см. `assets/README.md`) |
| `screen.py [сек] [папка]` | Запись экрана Flipper по USB (RPC screen stream): PNG + GIF |
| `press.py back [short\|long]` | Нажать кнопку Flipper через CLI (press + short + release) |
| `watch_log.py [сек]` | Поток лога прошивки, строки менеджера анимаций |

Особенности консоли Flipper:

- пути с пробелами брать в кавычки; имена с не-ASCII символами адресовать нельзя;
- `storage remove` удаляет только файлы и пустые папки, рекурсия на стороне ПК;
- `storage write_chunk` дописывает в конец, перед загрузкой файл нужно удалить.

Сборка собственных приложений: `ufbt` уже установлен, SDK Unleashed берётся так:

```bash
ufbt update --index-url=https://up.unleashedflip.com/directory.json --channel=release
```

Нажатия через CLI: одиночное `input send back short` прошивка игнорирует, нужна последовательность
press → short → release (это делает `press.py`).

Приложение-монитор DedSec Uplink и его компаньон для ПК: `apps/dedsec_uplink`, `uplink/README.md`.
