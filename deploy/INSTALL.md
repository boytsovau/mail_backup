# Установка на Ubuntu 26.04

Установку и обновление выполняет `deploy/install.sh`. Повторный запуск безопасен:
ключи, БД, файл настроек и бэкапы он не трогает.

| Что | Где |
|---|---|
| Код и venv | `/opt/mail_backup` (git-клон, владелец — ваш пользователь) |
| Данные (БД, логи, учётки Dovecot) | `/var/lib/mail_backup` |
| Бэкапы | `/srv/mail_backup` (в PROD — отдельный диск) |
| Настройки и ключи | `/etc/mail_backup` |
| Пользователь сервиса | `mailbackup` |
| Команда приложения | `mail-backup` (обёртка в `/usr/local/bin`, сама переключается на `mailbackup`) |

## Код на сервере (git)

Клонировать **от своего пользователя, без sudo**: SSH-ключ к репозиторию лежит у него,
у root ключа нет. Каталог один раз создаётся под своим пользователем (права 755 —
чтобы пользователь сервиса `mailbackup` мог читать код):

```bash
sudo install -d -o "$USER" -g "$USER" -m 0755 /opt/mail_backup
git clone <URL-репозитория> /opt/mail_backup
```

`.venv` и `*.egg-info`, которые установщик создаёт от root, есть в `.gitignore` —
`git pull` их не трогает.

## DEV — тестовый сервер без отдельного диска

```bash
sudo /opt/mail_backup/deploy/install.sh --mode dev
```

Бэкапы лягут в `/srv/mail_backup` на системном диске — установщик предупредит об этом.

## PROD — бэкапы на отдельном диске

Сначала смонтировать диск в `/srv/mail_backup` (пример для нового диска `/dev/sdb`):

```bash
sudo mkfs.ext4 -L mailbackup /dev/sdb          # ВНИМАНИЕ: стирает диск
sudo mkdir -p /srv/mail_backup
echo "UUID=$(sudo blkid -s UUID -o value /dev/sdb) /srv/mail_backup ext4 defaults,noatime 0 2" \
  | sudo tee -a /etc/fstab
sudo mount /srv/mail_backup && findmnt /srv/mail_backup
```

Затем склонировать код (см. выше) и:

```bash
sudo /opt/mail_backup/deploy/install.sh --mode prod
```

Защита от записи бэкапов мимо диска:

- установщик в режиме `prod` прерывается, если `/srv/mail_backup` не точка монтирования;
- Dovecot получает `RequiresMountsFor=/srv/mail_backup` и не стартует без диска;
- приложение перед каждым бэкапом проверяет, что диск смонтирован, иначе запуск — «сбой».

Другой путь к диску: `--backup-root /mnt/backup`. Режим и путь запоминаются
в `/etc/mail_backup/mail_backup.env`; повторный запуск с другими значениями
установщик отклонит, чтобы случайно не переключить PROD в DEV.

## Что делает установщик

1. Проверяет ОС, режим и (в PROD) диск.
2. Ставит пакеты: git, python3-venv, dovecot-imapd, Perl-модули для imapsync.
   Пакеты, которых нет в репозиториях, пропускает с предупреждением.
3. Скачивает imapsync 2.314 по тегу с GitHub, сверяет SHA256, проверяет запуск.
4. Создаёт пользователя `mailbackup` и каталоги с нужными правами.
5. Создаёт `/etc/mail_backup/mail_backup.env` (только при первой установке).
6. Ставит приложение в venv, создаёт ключи и БД (`mail-backup init`).
7. Ставит конфиг Dovecot из `deploy/dovecot/dovecot.conf` (сначала проверяет
   `doveconf`; оригинал пакета сохраняется как `dovecot.conf.orig-mail_backup`),
   перезапускает Dovecot и проверяет, что он слушает только `127.0.0.1:143`.

## Обновление

```bash
cd /opt/mail_backup && git pull                         # без sudo
sudo ./deploy/install.sh --mode dev                     # или prod — тот же, что при установке
```

## Проверка этапа 2 на одном ящике

Для Яндекса и Mail.ru нужен **пароль приложения** (подсказка печатается при добавлении).

```bash
mail-backup mailbox-add user@yandex.ru --provider yandex   # спросит пароль
mail-backup mailbox-list

mail-backup backup 1 --check   # только вход на хостер и в локальный Dovecot
mail-backup backup 1 --dry     # пробный прогон: что было бы скопировано
mail-backup backup 1           # настоящий бэкап
mail-backup backup 1           # повтор: должно быть «Писем перенесено: 0»
```

Что проверить:

- В веб-интерфейсе хостера непрочитанные письма **остались непрочитанными**,
  ничего не удалено и не перемещено.
- Письма лежат в хранилище (числа совпадают с хостером, включая непрочитанные):
  `sudo doveadm mailbox status -u user@yandex.ru 'messages unseen' '*'`
- Кириллические папки («Отправленные», «Черновики») на месте:
  `sudo doveadm mailbox list -u user@yandex.ru`
- Файлов паролей не осталось: `sudo ls /var/lib/mail_backup/tmp/` — пусто.

## Если что-то пошло не так

Прислать:

- вывод установщика (последний экран);
- при ошибке Dovecot: `sudo journalctl -u dovecot -n 50`;
- при ошибке бэкапа: вывод `mail-backup backup ...` и последние ~50 строк лога
  (путь к логу печатается в конце).
