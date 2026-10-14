# Ручная установка на Ubuntu 26.04

Временная инструкция для поэтапной проверки. На этапе 7 её заменит скрипт установки.
Все команды выполняются от root (или через `sudo`).

Пути по умолчанию:

| Что | Где |
|---|---|
| Код и venv | `/opt/mail_backup` |
| Данные (БД, логи, учётки Dovecot) | `/var/lib/mail_backup` |
| Бэкапы (отдельный диск) | `/srv/mail_backup` |
| Настройки и ключи | `/etc/mail_backup` |
| Пользователь сервиса | `mailbackup` |

## 1. Диск под бэкапы

Отдельный диск монтируется в `/srv/mail_backup` через `/etc/fstab`
(по UUID, `blkid` покажет его). Проверка: `findmnt /srv/mail_backup`.

## 2. Пакеты

```bash
apt update
apt install -y git python3 python3-venv dovecot-imapd

# Зависимости imapsync (список из официальной инструкции imapsync для Ubuntu).
# Если какого-то пакета в 26.04 нет — убрать его из списка: на шаге 3
# imapsync сам напишет, каких модулей Perl ему не хватает.
apt install -y \
  libauthen-ntlm-perl libclass-load-perl libcrypt-openssl-rsa-perl libcrypt-ssleay-perl \
  libdata-uniqid-perl libdigest-hmac-perl libdist-checkconflicts-perl libencode-imaputf7-perl \
  libfile-copy-recursive-perl libfile-tail-perl libio-compress-perl libio-socket-inet6-perl \
  libio-socket-ssl-perl libio-tee-perl libjson-webtoken-perl libmail-imapclient-perl \
  libmodule-scandeps-perl libnet-dbus-perl libnet-dns-perl libnet-ssleay-perl \
  libpar-packer-perl libproc-processtable-perl libreadonly-perl libregexp-common-perl \
  libsys-meminfo-perl libterm-readkey-perl libtest-fatal-perl libtest-mock-guard-perl \
  libtest-mockobject-perl libtest-pod-perl libtest-requires-perl libtest-simple-perl \
  libunicode-string-perl liburi-perl libtest-nowarnings-perl libtest-deep-perl \
  libtest-warn-perl make time cpanminus
```

## 3. imapsync (фиксированная версия)

```bash
IMAPSYNC_VERSION=2.314
wget -O /usr/local/bin/imapsync \
  "https://raw.githubusercontent.com/imapsync/imapsync/imapsync-${IMAPSYNC_VERSION}/imapsync"
chmod 755 /usr/local/bin/imapsync
imapsync --version     # должно напечатать 2.314
imapsync               # без аргументов: проверка модулей Perl и справка
```

## 4. Пользователь и каталоги

```bash
adduser --system --group --home /var/lib/mail_backup mailbackup
# Dovecot должен пройти в /var/lib/mail_backup до файла учёток, но не листать каталог
chmod 0711 /var/lib/mail_backup

# Каталог учёток Dovecot: группа dovecot + setgid, чтобы новые файлы
# получали группу dovecot и Dovecot мог их читать
install -d -o mailbackup -g dovecot -m 2750 /var/lib/mail_backup/dovecot

install -d -o mailbackup -g mailbackup -m 0700 /srv/mail_backup /etc/mail_backup
```

## 5. Код и venv

Код из git-репозитория проекта:

```bash
git clone <URL-репозитория> /opt/mail_backup
python3 -m venv /opt/mail_backup/.venv
/opt/mail_backup/.venv/bin/pip install -e /opt/mail_backup
```

Обновление на следующих этапах: `cd /opt/mail_backup && git pull && .venv/bin/pip install -e .`

## 6. Настройки

```bash
cat > /etc/mail_backup/mail_backup.env <<'EOF'
MB_DATA_DIR=/var/lib/mail_backup
MB_BACKUP_ROOT=/srv/mail_backup
MB_SECRETS_DIR=/etc/mail_backup
MB_IMAPSYNC_BIN=/usr/local/bin/imapsync
EOF
chown mailbackup:mailbackup /etc/mail_backup/mail_backup.env
chmod 600 /etc/mail_backup/mail_backup.env

# Ярлык: все команды приложения — от имени mailbackup
alias mb='sudo -u mailbackup -H /opt/mail_backup/.venv/bin/mail-backup'

mb init     # каталоги, ключи, БД
mb check    # покажет пути и версию imapsync
```

## 7. Dovecot

```bash
cp /etc/dovecot/dovecot.conf /etc/dovecot/dovecot.conf.orig
sed -e "s|@BACKUP_ROOT@|/srv/mail_backup|g" \
    -e "s|@DATA_DIR@|/var/lib/mail_backup|g" \
    -e "s|@SERVICE_USER@|mailbackup|g" \
    -e "s|@SERVICE_UID@|$(id -u mailbackup)|g" \
    /opt/mail_backup/deploy/dovecot/dovecot.conf > /etc/dovecot/dovecot.conf

doveconf -n > /dev/null && echo "конфиг OK"   # синтаксическая проверка
systemctl restart dovecot
ss -ltnp | grep ':143'                         # должен слушать только 127.0.0.1:143
```

Если `doveconf` ругается — прислать его вывод: шаблон написан по документации 2.4,
но на живой 26.04 ещё не проверялся.

## 8. Проверка этапа 2 на одном ящике

Для Яндекса и Mail.ru нужен **пароль приложения** (подсказка печатается при добавлении).

```bash
mb mailbox-add user@yandex.ru --provider yandex   # спросит пароль
mb mailbox-list

mb backup 1 --check   # только вход на хостер и в локальный Dovecot
mb backup 1 --dry     # пробный прогон: что было бы скопировано
mb backup 1           # настоящий бэкап
mb backup 1           # повтор: должно быть «Писем перенесено: 0»
```

Что проверить:

- В веб-интерфейсе хостера непрочитанные письма **остались непрочитанными**,
  ничего не удалено и не перемещено.
- Письма лежат в хранилище:
  `doveadm mailbox status -u user@yandex.ru 'messages unseen' '*'` —
  числа должны совпадать с хостером, включая непрочитанные.
- Кириллические папки («Отправленные», «Черновики») на месте:
  `doveadm mailbox list -u user@yandex.ru`.
- Лог запуска: путь печатается в конце `mb backup`, каталог `/var/lib/mail_backup/logs/`.
- Файлов паролей не осталось: `ls /var/lib/mail_backup/tmp/` — пусто.

При ошибке прислать вывод `mb backup` и последние ~50 строк лога запуска.
