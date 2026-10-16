#!/usr/bin/env bash
# Применяет к Dovecot адрес просмотра почты, заданный в панели («Настройки» → «Просмотр почты»).
#
# Веб работает без прав root и только пишет IP в файл запроса; этот помощник запускает
# systemd от root (mail-backup-dovecot-listen.path — при изменении файла). Он проверяет
# адрес, пишет LISTEN_CONF (его подключает конфиг Dovecot), перезагружает Dovecot,
# убеждается, что тот слушает ровно 127.0.0.1 и этот адрес, и пишет итог для панели.
# Не вышло — возвращает прежний файл.
#
# Файлы запроса и итога лежат в каталоге пользователя сервиса — их содержимому не доверяем:
# принимаем только IPv4-адрес с интерфейса сервера, ссылки не читаем, в итог пишем только
# свои сообщения, а сам итог кладём переименованием (запись по подложенной ссылке невозможна).
#
#   mail-backup-dovecot-listen КАТАЛОГ_ДАННЫХ [--no-reload]
#
# --no-reload — только записать LISTEN_CONF (установщик сам перезапускает Dovecot).
# Устанавливается копией в /usr/local/sbin (deploy/install.sh), а не запускается из
# каталога с кодом: тот принадлежит не root.
set -euo pipefail

DATA_DIR=${1:?Использование: $0 КАТАЛОГ_ДАННЫХ [--no-reload]}
NO_RELOAD=0
[[ ${2:-} == --no-reload ]] && NO_RELOAD=1
REQUEST=$DATA_DIR/dovecot/listen
STATUS=$DATA_DIR/dovecot/listen.status
# Переменная окружения — только для тестов (tests/test_dovecot_listen.py)
LISTEN_CONF=${MB_LISTEN_CONF:-/etc/dovecot/mail_backup_listen.conf}
PORT=143

# Итог для панели: $1 — ok|error, $2 — сообщение, $3 — обработанный запрос
# (не передан — запрос не удалось прочитать или это не адрес)
write_status() {
    local tmp
    tmp=$(mktemp "$DATA_DIR/dovecot/.listen.status.XXXXXX")
    {
        echo "state=$1"
        echo "message=$2"
        [[ $# -ge 3 ]] && echo "requested=$3"
        echo "time=$(date +%s)"
    } >"$tmp"
    chmod 0644 "$tmp"
    mv -f "$tmp" "$STATUS"
}

# Запрос → ADDR (пусто — только 127.0.0.1). Нет файла — только локально
read_request() {
    ADDR=""
    [[ -e $REQUEST || -L $REQUEST ]] || return 0
    [[ -f $REQUEST && ! -L $REQUEST ]] || return 1
    ADDR=$(head -c 64 "$REQUEST" | head -n 1 | tr -d '[:space:]')
}

is_ipv4() {
    local octet
    [[ $1 =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
    for octet in ${1//./ }; do
        ((10#$octet <= 255)) || return 1
    done
}

server_has_address() {
    ip -o -4 addr show | awk '{print $4}' | cut -d/ -f1 | grep -qxF "$1"
}

write_conf() {
    local tmp
    tmp=$(mktemp "$(dirname "$LISTEN_CONF")/.mail_backup_listen.XXXXXX")
    {
        echo "# Создано mail-backup-dovecot-listen по настройке панели — вручную не править."
        echo "# imapsync ходит на 127.0.0.1; второй адрес — для просмотра почты клиентом"
        echo "listen = 127.0.0.1${1:+, $1}"
    } >"$tmp"
    chmod 0644 "$tmp"
    mv -f "$tmp" "$LISTEN_CONF"
}

# Dovecot слушает порт 143 ровно на 127.0.0.1 и ADDR (если задан) — ждём до 5 секунд
listening_ok() {
    local want got _
    want=$(printf '%s\n' "127.0.0.1:$PORT" ${ADDR:+"$ADDR:$PORT"} | sort)
    for _ in 1 2 3 4 5; do
        got=$(ss -Hltn "sport = :$PORT" | awk '{print $4}' | sort -u)
        [[ $got == "$want" ]] && return 0
        sleep 1
    done
    return 1
}

restore_prev() {
    if [[ -f $LISTEN_CONF.prev ]]; then
        mv -f "$LISTEN_CONF.prev" "$LISTEN_CONF"
    else
        write_conf ""
    fi
}

# Возвращает 0 при успехе; MESSAGE — что сообщить в панель
apply() {
    MESSAGE=""
    if [[ -f $LISTEN_CONF ]]; then
        cp -p "$LISTEN_CONF" "$LISTEN_CONF.prev"
    else
        rm -f "$LISTEN_CONF.prev"
    fi
    write_conf "$ADDR"
    ((NO_RELOAD)) && return 0

    if ! doveconf -n >/dev/null 2>&1; then
        restore_prev
        MESSAGE="конфиг Dovecot не прошёл проверку (doveconf -n)"
        return 1
    fi
    if ! systemctl is-active -q dovecot; then
        MESSAGE="Dovecot сейчас не запущен — адрес применится при его запуске"
        return 0
    fi
    # reload не рвёт соединения идущих бэкапов; если слушающие адреса не сменились — restart
    systemctl reload dovecot && listening_ok && return 0
    systemctl restart dovecot && listening_ok && return 0

    restore_prev
    systemctl restart dovecot || true
    MESSAGE="Dovecot не открыл порт $PORT на новом адресе (журнал: journalctl -u dovecot)"
    return 1
}

# Запрос могли поменять, пока применяли прошлый: повторный запуск в это время path-юнит
# пропускает — поэтому сверяемся с файлом и при изменении применяем снова
for _ in 1 2 3 4 5; do
    if ! read_request; then
        [[ -f $LISTEN_CONF ]] || write_conf ""
        write_status error "файл запроса — не обычный файл"
        exit 1
    fi
    if [[ -n $ADDR ]]; then
        error=""
        if ! is_ipv4 "$ADDR"; then
            # Не адрес — в итог содержимое не пишем
            [[ -f $LISTEN_CONF ]] || write_conf ""
            write_status error "в запросе не IPv4-адрес"
            exit 1
        elif [[ $ADDR == 127.* || $ADDR == 0.* ]]; then
            error="нужен адрес интерфейса сервера, а не $ADDR"
        elif ! server_has_address "$ADDR"; then
            error="адреса $ADDR нет на этом сервере (ip -4 addr)"
        fi
        if [[ -n $error ]]; then
            # Прежний адрес остаётся; на новой установке — только локально
            [[ -f $LISTEN_CONF ]] || write_conf ""
            write_status error "$error" "$ADDR"
            exit 1
        fi
    fi

    if apply; then
        write_status ok "$MESSAGE" "$ADDR"
    else
        write_status error "$MESSAGE" "$ADDR"
        exit 1
    fi
    applied=$ADDR
    read_request || exit 0
    [[ $ADDR == "$applied" ]] && exit 0
done
