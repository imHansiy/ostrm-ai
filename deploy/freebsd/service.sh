#!/bin/sh
# Run only the OStrm AI application; the original OpenList remains untouched.
set -eu
PATH="/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin${PATH:+:$PATH}"
export PATH
ROOT=$(CDPATH= cd -P "$(dirname "$0")/../.." && pwd)
RUN="$ROOT/runtime"
PYTHON="$ROOT/.venv/bin/python"
mkdir -p "$RUN"
cd "$ROOT"
alive() { /usr/bin/pgrep -L -F "$RUN/$1-supervisor.pid" >/dev/null 2>&1; }
start_one() {
    name=$1
    if ! alive "$name"; then
        /usr/sbin/daemon -r -R 5 -P "$RUN/$name-supervisor.pid" -p "$RUN/$name.pid" \
            -o "$RUN/$name.log" -t "ostrm-ai-$name" "$PYTHON" "$ROOT/deploy/freebsd/launch.py" "$name" </dev/null >/dev/null 2>&1
    fi
}
stop_one() {
    if alive "$1"; then
        /usr/bin/pkill -TERM -L -F "$RUN/$1-supervisor.pid"
        count=0
        while alive "$1" && [ "$count" -lt 45 ]; do sleep 1; count=$((count+1)); done
        if alive "$1"; then printf '%s is still stopping; retry after requests finish\n' "$1" >&2; return 1; fi
    fi
}
case "${1:-status}" in
    start) exec /usr/bin/lockf -s -t 0 "$RUN/start.lock" /bin/sh "$0" _start ;;
    _start)
        umask 077
        if [ -f "$RUN/app.disabled" ]; then exit 0; fi
        "$PYTHON" deploy/freebsd/launch.py prepare
        start_one ai
        start_one java
        start_one gateway
        ;;
    stop) stop_one gateway; stop_one java; stop_one ai ;;
    restart-gateway) stop_one gateway; exec /bin/sh "$0" start ;;
    restart-ai) stop_one ai; exec /bin/sh "$0" start ;;
    status)
        for name in ai java gateway; do
            child=$(cat "$RUN/$name.pid" 2>/dev/null || true)
            case "$child" in ''|*[!0-9]*) child=0 ;; esac
            if alive "$name" && [ "$child" -gt 1 ] && kill -0 "$child" 2>/dev/null; then
                printf '%s running\n' "$name"
            elif alive "$name"; then printf '%s restarting\n' "$name"
            else printf '%s stopped\n' "$name"; fi
        done
        ;;
    *) printf 'Usage: %s start|stop|restart-gateway|restart-ai|status\n' "$0" >&2; exit 2 ;;
esac
