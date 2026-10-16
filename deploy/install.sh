#!/usr/bin/env bash
# Установка и обновление mail_backup на Ubuntu 26.04.
#
#   sudo ./deploy/install.sh --mode dev    # тестовый сервер, бэкапы на системном диске
#   sudo ./deploy/install.sh --mode prod   # бэкапы только на отдельном смонтированном диске
#
# Повторный запуск безопасен: им же обновляемся после `git pull`.
# Ключи, БД, файл настроек и бэкапы при повторном запуске не трогаются.
set -euo pipefail

# --- Параметры по умолчанию ---------------------------------------------------
MODE=""
BACKUP_ROOT=/srv/mail_backup
DATA_DIR=/var/lib/mail_backup
ETC_DIR=/etc/mail_backup
ENV_FILE=$ETC_DIR/mail_backup.env
SERVICE_USER=mailbackup
IMAPSYNC_VERSION=2.314
# SHA256 файла imapsync из тега imapsync-2.314 на GitHub (проверено при фиксации версии)
IMAPSYNC_SHA256=89faed96f7c389723ddd7e78a00421b90d5b85bf79407fcf1b11209f3d4d7416
IMAPSYNC_BIN=/usr/local/bin/imapsync
WRAPPER=/usr/local/bin/mail-backup
# Root-помощник: применяет к Dovecot адрес просмотра почты, заданный в панели
LISTEN_HELPER=/usr/local/sbin/mail-backup-dovecot-listen
LISTEN_CONF=/etc/dovecot/mail_backup_listen.conf
APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Веб-панель слушает только localhost, наружу её отдаёт nginx по HTTPS
WEB_PORT=8080
# Адрес, по которому админ открывает панель (IP или имя); по умолчанию — IP сервера
PUBLIC_HOST=""
SSL_DIR=/etc/nginx/ssl
SSL_CERT=$SSL_DIR/mail_backup.crt
SSL_KEY=$SSL_DIR/mail_backup.key

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Использование: sudo $0 --mode dev|prod [--backup-root ПУТЬ] [--host АДРЕС]

  --mode dev          тестовый сервер: отдельный диск не обязателен
  --mode prod         рабочий сервер: --backup-root обязан быть точкой
                      монтирования отдельного диска, иначе установка прервётся
  --backup-root ПУТЬ  где хранить бэкапы (по умолчанию $BACKUP_ROOT)
  --host АДРЕС        IP или имя, по которому открывается панель
                      (по умолчанию — первый IP сервера)

Адрес, на котором Dovecot принимает почтовый клиент для просмотра бэкапов,
задаётся в панели: «Настройки» → «Просмотр почты».
EOF
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --mode) MODE=${2:-}; shift 2 ;;
        --backup-root) BACKUP_ROOT=${2:-}; shift 2 ;;
        --host) PUBLIC_HOST=${2:-}; shift 2 ;;
        --imap-listen) die "--imap-listen больше нет: адрес просмотра почты задаётся в панели («Настройки» → «Просмотр почты»)" ;;
        -h|--help) usage 0 ;;
        *) warn "Неизвестный аргумент: $1"; usage 1 ;;
    esac
done
[[ $MODE == dev || $MODE == prod ]] || usage 1
BACKUP_ROOT=${BACKUP_ROOT%/}
[[ $BACKUP_ROOT == /* ]] || die "--backup-root должен быть абсолютным путём"

# --- Предварительные проверки ---------------------------------------------------
check_system() {
    [[ $EUID -eq 0 ]] || die "Запускать от root: sudo $0 ..."
    # shellcheck source=/dev/null
    . /etc/os-release
    [[ ${ID:-} == ubuntu ]] || die "Поддерживается только Ubuntu (обнаружено: ${PRETTY_NAME:-?})"
    [[ ${VERSION_ID:-} == 26.04 ]] || warn "Скрипт рассчитан на Ubuntu 26.04, у вас ${VERSION_ID:-?}"
}

# Существующий файл настроек — источник истины: случайный повторный запуск
# с другим режимом или путём не должен переключить prod в dev или «потерять» бэкапы
check_existing_env() {
    [[ -f $ENV_FILE ]] || return 0
    local cur_mode cur_root
    cur_mode=$(sed -n 's/^MB_DEPLOY_MODE=//p' "$ENV_FILE")
    cur_root=$(sed -n 's/^MB_BACKUP_ROOT=//p' "$ENV_FILE")
    if [[ -n $cur_mode && $cur_mode != "$MODE" ]]; then
        die "Сервер уже установлен в режиме '$cur_mode' ($ENV_FILE). Смена режима — только вручную."
    fi
    if [[ -n $cur_root && $cur_root != "$BACKUP_ROOT" ]]; then
        die "Бэкапы уже настроены в '$cur_root' ($ENV_FILE). Перенос хранилища — только вручную."
    fi
}

check_backup_disk() {
    if [[ $MODE == prod ]]; then
        mountpoint -q "$BACKUP_ROOT" || die "PROD: $BACKUP_ROOT не является точкой монтирования.
    Смонтируйте отдельный диск (запись в /etc/fstab по UUID) и запустите снова."
        findmnt --fstab "$BACKUP_ROOT" >/dev/null ||
            warn "$BACKUP_ROOT смонтирован, но не прописан в /etc/fstab — после перезагрузки диска не будет"
        log "Диск для бэкапов: $(findmnt -no SOURCE,FSTYPE,SIZE "$BACKUP_ROOT" 2>/dev/null || echo "$BACKUP_ROOT")"
    elif mountpoint -q "$BACKUP_ROOT" 2>/dev/null; then
        log "DEV: $BACKUP_ROOT — отдельный диск"
    else
        warn "DEV: бэкапы будут лежать на системном диске ($BACKUP_ROOT). Для PROD нужен отдельный диск."
    fi
}

# --- Пакеты -------------------------------------------------------------------------
# restic — снимки и выгрузка (нужен ≥ 0.16); openssh-client — ssh/ssh-keygen для выгрузки по SFTP
BASE_PACKAGES=(git curl ca-certificates openssl python3 python3-venv dovecot-imapd nginx restic openssh-client)
RESTIC_MIN_VERSION=0.16

# Зависимости imapsync — из официальной инструкции imapsync для Ubuntu
IMAPSYNC_PERL_PACKAGES=(
    libauthen-ntlm-perl libclass-load-perl libcrypt-openssl-rsa-perl libcrypt-ssleay-perl
    libdata-uniqid-perl libdigest-hmac-perl libdist-checkconflicts-perl libencode-imaputf7-perl
    libfile-copy-recursive-perl libfile-tail-perl libio-compress-perl libio-socket-inet6-perl
    libio-socket-ssl-perl libio-tee-perl libjson-webtoken-perl libmail-imapclient-perl
    libmodule-scandeps-perl libnet-dbus-perl libnet-dns-perl libnet-ssleay-perl
    libpar-packer-perl libproc-processtable-perl libreadonly-perl libregexp-common-perl
    libsys-meminfo-perl libterm-readkey-perl libtest-fatal-perl libtest-mock-guard-perl
    libtest-mockobject-perl libtest-pod-perl libtest-requires-perl libtest-simple-perl
    libunicode-string-perl liburi-perl libtest-nowarnings-perl libtest-deep-perl
    libtest-warn-perl make time cpanminus
)

install_packages() {
    log "Установка пакетов"
    # nginx, поставленный этим скриптом, можно настраивать целиком;
    # уже стоявший — трогаем осторожно (там могут быть чужие сайты)
    if dpkg -s nginx >/dev/null 2>&1; then NGINX_PREINSTALLED=1; else NGINX_PREINSTALLED=0; fi
    apt-get update -q
    # Пакеты, которых нет в этой версии Ubuntu, пропускаем: если без них
    # imapsync не запустится, это покажет проверка в install_imapsync
    local available=() missing=() pkg
    for pkg in "${IMAPSYNC_PERL_PACKAGES[@]}"; do
        if apt-cache show "$pkg" >/dev/null 2>&1; then
            available+=("$pkg")
        else
            missing+=("$pkg")
        fi
    done
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${BASE_PACKAGES[@]}" "${available[@]}"
    if [[ ${#missing[@]} -gt 0 ]]; then
        warn "Нет в репозиториях, пропущены: ${missing[*]}"
    fi
}

install_imapsync() {
    if [[ -x $IMAPSYNC_BIN ]] && [[ $("$IMAPSYNC_BIN" --version 2>/dev/null) == "$IMAPSYNC_VERSION" ]]; then
        log "imapsync $IMAPSYNC_VERSION уже установлен"
        return
    fi
    log "Установка imapsync $IMAPSYNC_VERSION"
    local tmp
    tmp=$(mktemp)
    curl -fsSL -o "$tmp" \
        "https://raw.githubusercontent.com/imapsync/imapsync/imapsync-${IMAPSYNC_VERSION}/imapsync"
    echo "$IMAPSYNC_SHA256  $tmp" | sha256sum -c --quiet ||
        { rm -f "$tmp"; die "Контрольная сумма imapsync не совпала — файл не установлен"; }
    install -m 0755 "$tmp" "$IMAPSYNC_BIN"
    rm -f "$tmp"

    local out
    # Если не хватает модулей Perl, imapsync упадёт уже на загрузке и перечислит их
    out=$("$IMAPSYNC_BIN" --version 2>&1) ||
        die "imapsync не запускается (не хватает модулей Perl?):
$out"
    log "imapsync $out"
}

check_restic() {
    local ver
    ver=$(restic version 2>/dev/null | awk '{print $2}')
    [[ -n $ver ]] || die "restic не установился (apt install restic)"
    if [[ $(printf '%s\n%s\n' "$RESTIC_MIN_VERSION" "$ver" | sort -V | head -1) != "$RESTIC_MIN_VERSION" ]]; then
        warn "restic $ver старее $RESTIC_MIN_VERSION — снимки работать не будут, обновите restic"
    else
        log "restic $ver"
    fi
}

# --- Пользователь, каталоги, приложение --------------------------------------------
setup_user_and_dirs() {
    if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
        log "Создание пользователя $SERVICE_USER"
        useradd --system --user-group --home-dir "$DATA_DIR" --no-create-home \
            --shell /usr/sbin/nologin "$SERVICE_USER"
    fi
    # Dovecot должен пройти в каталог данных до файла учёток, но не листать его
    install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0711 "$DATA_DIR"
    # Группа dovecot + setgid: новый файл учёток получает группу dovecot и читается им
    install -d -o "$SERVICE_USER" -g dovecot -m 2750 "$DATA_DIR/dovecot"
    install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$BACKUP_ROOT" "$ETC_DIR"

    sudo -u "$SERVICE_USER" test -r "$APP_DIR/pyproject.toml" ||
        die "Пользователь $SERVICE_USER не может читать код в $APP_DIR.
    Разместите репозиторий в /opt/mail_backup и запустите установщик оттуда."
}

write_env_file() {
    if [[ -f $ENV_FILE ]]; then
        log "Файл настроек $ENV_FILE уже есть — не трогаю"
        return
    fi
    log "Создание $ENV_FILE"
    install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0600 /dev/null "$ENV_FILE"
    cat >"$ENV_FILE" <<EOF
# Создано deploy/install.sh. Только базовые пути — остальное настраивается в веб-панели.
MB_DEPLOY_MODE=$MODE
MB_DATA_DIR=$DATA_DIR
MB_BACKUP_ROOT=$BACKUP_ROOT
MB_SECRETS_DIR=$ETC_DIR
MB_IMAPSYNC_BIN=$IMAPSYNC_BIN
EOF
}

# Раньше адрес просмотра почты задавал установщик (MB_VIEW_IMAP_HOST). `mail-backup init`
# переносит его в настройки панели — после этого строка в env-файле не нужна
drop_legacy_imap_listen() {
    grep -q '^MB_VIEW_IMAP_HOST=' "$ENV_FILE" || return 0
    log "Адрес просмотра почты перенесён в панель — убираю MB_VIEW_IMAP_HOST из $ENV_FILE"
    sed -i '/^MB_VIEW_IMAP_HOST=/d' "$ENV_FILE"
}

install_app() {
    log "Установка приложения в venv ($APP_DIR/.venv)"
    [[ -x $APP_DIR/.venv/bin/python ]] || python3 -m venv "$APP_DIR/.venv"
    "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR"

    # Обёртка: команды приложения всегда выполняются от пользователя сервиса
    # и из корня — текущий каталог (например, /root) ему может быть недоступен
    cat >"$WRAPPER" <<EOF
#!/bin/sh
# Создано deploy/install.sh
cd / || exit 1
if [ "\$(id -un)" = "$SERVICE_USER" ]; then
    exec "$APP_DIR/.venv/bin/mail-backup" "\$@"
fi
exec sudo -u "$SERVICE_USER" -H "$APP_DIR/.venv/bin/mail-backup" "\$@"
EOF
    chmod 0755 "$WRAPPER"

    "$WRAPPER" init
    "$WRAPPER" dovecot-sync
}

# --- Dovecot ------------------------------------------------------------------------------
# IP просмотра почты, который сейчас в конфиге Dovecot (пусто — только 127.0.0.1)
imap_view_address() {
    sed -n 's/^listen = 127\.0\.0\.1, //p' "$LISTEN_CONF" 2>/dev/null || true
}

configure_dovecot() {
    local conf=/etc/dovecot/dovecot.conf tmp version template
    # У Dovecot 2.3 и 2.4 несовместимый синтаксис конфига — шаблон по версии
    version=$(dovecot --version | cut -d' ' -f1)
    case $version in
        2.3.*) template=dovecot-2.3.conf ;;
        2.4.*) template=dovecot-2.4.conf ;;
        *) die "Неподдерживаемая версия Dovecot: $version (нужна 2.3 или 2.4)" ;;
    esac
    log "Настройка Dovecot $version (шаблон $template)"
    # Оригинальный конфиг пакета сохраняем один раз
    [[ -e $conf.orig-mail_backup ]] || cp -a "$conf" "$conf.orig-mail_backup"

    # Адреса Dovecot — в $LISTEN_CONF: его пишет помощник по настройке из панели
    # (файл запроса создал `mail-backup init`). Адрес не годится — остаётся прежний
    # или только 127.0.0.1, установка не прерывается
    install -m 0755 "$APP_DIR/deploy/dovecot-listen.sh" "$LISTEN_HELPER"
    "$LISTEN_HELPER" "$DATA_DIR" --no-reload ||
        warn "Адрес просмотра почты не применён: $(sed -n 's/^message=//p' "$DATA_DIR/dovecot/listen.status") — выберите другой в панели"

    tmp=$(mktemp)
    sed -e "s|@BACKUP_ROOT@|$BACKUP_ROOT|g" \
        -e "s|@DATA_DIR@|$DATA_DIR|g" \
        -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
        -e "s|@SERVICE_UID@|$(id -u "$SERVICE_USER")|g" \
        "$APP_DIR/deploy/dovecot/$template" >"$tmp"
    # Глобальные ACL (только чтение для учёток просмотра): в 2.3 — отдельным файлом,
    # в 2.4 — внутри конфига
    if [[ $template == dovecot-2.3.conf ]]; then
        install -m 0644 "$APP_DIR/deploy/dovecot/mail_backup.acl" /etc/dovecot/mail_backup.acl
    fi

    local err
    if ! err=$(doveconf -c "$tmp" -n 2>&1 >/dev/null); then
        rm -f "$tmp"
        die "Конфиг Dovecot не прошёл проверку (рабочий конфиг не изменён):
$err"
    fi
    install -m 0644 "$tmp" "$conf"
    rm -f "$tmp"

    if [[ $MODE == prod ]]; then
        # Dovecot не стартует без диска с бэкапами — иначе он молча создал бы
        # ящики на системном диске под точкой монтирования
        install -d /etc/systemd/system/dovecot.service.d
        cat >/etc/systemd/system/dovecot.service.d/mail_backup.conf <<EOF
# Создано deploy/install.sh
[Unit]
RequiresMountsFor=$BACKUP_ROOT
EOF
    fi

    if [[ ! -d /run/systemd/system ]]; then
        warn "systemd не запущен (контейнер?) — Dovecot нужно перезапустить вручную"
        return
    fi
    systemctl daemon-reload
    systemctl enable -q dovecot
    systemctl restart dovecot

    # Ждём до 5 секунд, пока Dovecot откроет порт после перезапуска
    local _
    for _ in 1 2 3 4 5; do
        ss -ltn | grep -q '127\.0\.0\.1:143 ' && break
        sleep 1
    done
    ss -ltn | grep -q '127\.0\.0\.1:143 ' || die "Dovecot не слушает 127.0.0.1:143 — см. journalctl -u dovecot"
    local expected='127\.0\.0\.1:143 ' view_addr
    view_addr=$(imap_view_address)
    if [[ -n $view_addr ]]; then
        ss -ltn | grep -qF "$view_addr:143 " || die "Dovecot не слушает $view_addr:143 — см. journalctl -u dovecot"
        expected="$expected|${view_addr//./\\.}:143 "
        log "Просмотр почты клиентом: $view_addr:143 (адрес и сети — в панели, «Настройки»)"
    fi
    if ss -ltn | grep -E ':(143|993) ' | grep -Evq "$expected"; then
        warn "Dovecot слушает лишние адреса — проверьте: ss -ltnp | grep dovecot"
    fi
}

# --- Веб-панель и воркер (systemd), nginx ---------------------------------------------------
render_template() {
    # $1 — шаблон, $2 — куда записать
    local requires_mounts="# RequiresMountsFor не нужен в режиме dev"
    [[ $MODE == prod ]] && requires_mounts="RequiresMountsFor=$BACKUP_ROOT"
    sed -e "s|@APP_DIR@|$APP_DIR|g" \
        -e "s|@DATA_DIR@|$DATA_DIR|g" \
        -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
        -e "s|@WEB_PORT@|$WEB_PORT|g" \
        -e "s|@SSL_CERT@|$SSL_CERT|g" \
        -e "s|@SSL_KEY@|$SSL_KEY|g" \
        -e "s|@REQUIRES_MOUNTS@|$requires_mounts|g" \
        "$1" >"$2"
}

install_services() {
    if [[ ! -d /run/systemd/system ]]; then
        warn "systemd не запущен — сервисы mail-backup-web и mail-backup-worker не установлены"
        return
    fi
    log "Сервисы systemd: mail-backup-web, mail-backup-worker, mail-backup-dovecot-listen"
    local unit
    for unit in mail-backup-web.service mail-backup-worker.service \
        mail-backup-dovecot-listen.service mail-backup-dovecot-listen.path; do
        render_template "$APP_DIR/deploy/systemd/$unit" "/etc/systemd/system/$unit"
    done
    systemctl daemon-reload
    systemctl enable -q mail-backup-web mail-backup-worker
    # Адрес просмотра почты из панели применяется сам: path-юнит следит за файлом запроса
    systemctl enable -q mail-backup-dovecot-listen.path
    systemctl restart mail-backup-dovecot-listen.path
    # restart, а не start: при обновлении сервисы должны подхватить новый код
    systemctl restart mail-backup-web mail-backup-worker

    local _
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        curl -fsS "http://127.0.0.1:$WEB_PORT/health" >/dev/null 2>&1 && return
        sleep 1
    done
    die "Веб-панель не отвечает на 127.0.0.1:$WEB_PORT — см. journalctl -u mail-backup-web -n 50"
}

configure_nginx() {
    [[ -n $PUBLIC_HOST ]] || PUBLIC_HOST=$(hostname -I | awk '{print $1}')
    [[ -n $PUBLIC_HOST ]] || die "Не удалось определить IP сервера, укажите --host"

    # Самоподписанный сертификат: браузер один раз предупредит. Создаётся один раз;
    # для своего сертификата достаточно заменить файлы и перезагрузить nginx
    if [[ ! -f $SSL_CERT ]]; then
        log "Самоподписанный сертификат для $PUBLIC_HOST"
        local san="DNS:$PUBLIC_HOST"
        [[ $PUBLIC_HOST =~ ^[0-9.]+$|: ]] && san="IP:$PUBLIC_HOST"
        install -d -m 0755 "$SSL_DIR"
        openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -sha256 \
            -subj "/CN=$PUBLIC_HOST" -addext "subjectAltName=$san" \
            -keyout "$SSL_KEY" -out "$SSL_CERT" 2>/dev/null
        chmod 600 "$SSL_KEY"
    fi

    log "Настройка nginx"
    local site=/etc/nginx/sites-available/mail_backup link=/etc/nginx/sites-enabled/mail_backup
    render_template "$APP_DIR/deploy/nginx/mail_backup.conf" "$site"
    if [[ ${NGINX_PREINSTALLED:-1} == 0 ]]; then
        # Свежий nginx: стандартная заглушка занимает default_server — убираем
        rm -f /etc/nginx/sites-enabled/default
    fi
    ln -sf "$site" "$link"
    local err
    if ! err=$(nginx -t 2>&1); then
        rm -f "$link"
        die "nginx -t не прошёл (сайт mail_backup отключён, остальное не тронуто):
$err
Если на сервере уже есть сайты на портах 80/443 — их нужно согласовать вручную."
    fi
    if [[ -d /run/systemd/system ]]; then
        systemctl enable -q nginx
        systemctl reload-or-restart nginx
    fi
    if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
        warn "Включён ufw: откройте порты для панели — sudo ufw allow 80,443/tcp"
        warn "Для просмотра почты клиентом по IP — порт 143 только из сети VPN: sudo ufw allow from <сеть VPN> to any port 143 proto tcp"
    fi
}

# --- Запуск -------------------------------------------------------------------------------
check_system
check_existing_env
check_backup_disk
install_packages
install_imapsync
check_restic
setup_user_and_dirs
write_env_file
install_app
drop_legacy_imap_listen
configure_dovecot
install_services
configure_nginx

log "Проверка"
"$WRAPPER" check
SETUP_URL=$("$WRAPPER" setup-link --if-no-admin --base-url "https://$PUBLIC_HOST")

cat <<EOF

Готово. Режим: $MODE
  Панель:    https://$PUBLIC_HOST
  Код:       $APP_DIR
  Данные:    $DATA_DIR
  Бэкапы:    $BACKUP_ROOT
  Настройки: $ENV_FILE
  IMAP для просмотра: $(view_addr=$(imap_view_address); echo "${view_addr:-только 127.0.0.1}") (меняется в панели)
EOF
if [[ -n $SETUP_URL ]]; then
    cat <<EOF

Создайте администратора по одноразовой ссылке (действует 24 часа):

  $SETUP_URL

Браузер предупредит о самоподписанном сертификате — это ожидаемо.
Новая ссылка, если эта устареет:
  sudo mail-backup setup-link --base-url https://$PUBLIC_HOST
EOF
fi
