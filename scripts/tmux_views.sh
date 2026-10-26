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

trace_id=${1:-latest}
if [[ "$trace_id" != latest && ! "$trace_id" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "Trace ID may contain only letters, digits, _ and -" >&2
    exit 2
fi
if [[ "$trace_id" == latest ]]; then
    trace_command="make -C '$ssm_dir' task-trace"
else
    trace_command="make -C '$ssm_dir' task-trace ID='$trace_id'"
fi

if has_window task-trace; then
    current=$(tmux show-window-options -v -t "$session:task-trace" @ssm_trace_id 2>/dev/null || true)
    if [[ "$current" != "$trace_id" ]]; then
        tmux respawn-window -k -t "$session:task-trace" "$trace_command"
        echo "task-trace now follows $trace_id"
    fi
else
    add_window task-trace "$trace_command"
fi
tmux set-window-option -t "$session:task-trace" @ssm_trace_id "$trace_id"
add_window errors "make -C '$ssm_dir' errors"
add_window go2-state "make -C '$ssm_dir' go2-state"
add_window mqtt-trace "make -C '$ssm_dir' trace"
if [[ -n "${SERIAL_PORT:-}" ]]; then
    add_window esp32-serial "mpremote connect '$SERIAL_PORT' repl"
fi
