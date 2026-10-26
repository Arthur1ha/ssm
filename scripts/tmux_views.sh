#!/usr/bin/env bash
# Add diagnostic windows to an existing tmux session. Never touch service windows.
set -euo pipefail

ssm_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
session=${SSM_TMUX_SESSION:-}
if [[ -z "$session" ]]; then
    if [[ -z "${TMUX:-}" ]]; then
        echo "Run inside tmux or set SSM_TMUX_SESSION to an existing session" >&2
        exit 2
    fi
    session=$(tmux display-message -p '#S')
fi
tmux has-session -t "$session"

has_window() {
    tmux list-windows -t "$session" -F '#{window_name}' | grep -Fxq "$1"
}

add_window() {
    local name=$1 command=$2
    if ! has_window "$name"; then
        tmux new-window -d -t "$session:" -n "$name" "$command"
        echo "Added $session:$name"
    fi
}

view_id=${1:-latest}
if [[ "$view_id" != latest && ! "$view_id" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "Session ID may contain only letters, digits, _ and -" >&2
    exit 2
fi
if [[ "$view_id" == latest ]]; then
    trace_command="make -C '$ssm_dir' task-trace"
else
    trace_command="make -C '$ssm_dir' task-trace ID='$view_id'"
fi

if has_window session_trace; then
    current=$(tmux show-window-options -v -t "$session:session_trace" @ssm_session_id 2>/dev/null || true)
    if [[ "$current" != "$view_id" ]]; then
        tmux respawn-window -k -t "$session:session_trace" "$trace_command"
        echo "session_trace now follows $view_id"
    fi
else
    add_window session_trace "$trace_command"
fi
tmux set-window-option -t "$session:session_trace" @ssm_session_id "$view_id"
add_window errors "make -C '$ssm_dir' errors"
add_window api-log "tail -n 80 -F '$ssm_dir/logs/api.log'"
add_window mqtt-trace "make -C '$ssm_dir' trace"
if [[ -n "${SERIAL_PORT:-}" ]]; then
    add_window esp32-serial "mpremote connect '$SERIAL_PORT' repl"
fi
