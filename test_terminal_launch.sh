#!/bin/bash
# Test suite for launch_terminal failure handling

set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP_DIR=$(mktemp -d /tmp/shuttle-terminal-test-XXXXXX)

cleanup() {
    /bin/rm -rf "$TMP_DIR"
}
trap cleanup EXIT

awk '
    /^terminal_errors_are_dbus_stale\(\)/ { copy = 1 }
    /^# Format seconds/ { exit }
    copy { print }
' "$ROOT_DIR/bin/shuttle" >"$TMP_DIR/terminal_functions.sh"

# shellcheck disable=SC1090
source "$TMP_DIR/terminal_functions.sh"

test_count=0
pass_count=0
fail_count=0

reset_shims() {
    /bin/rm -rf "$TMP_DIR/bin"
    /bin/mkdir -p "$TMP_DIR/bin"
    /bin/ln -s /usr/bin/cat "$TMP_DIR/bin/cat"
    /bin/ln -s /usr/bin/grep "$TMP_DIR/bin/grep"
    /bin/ln -s /usr/bin/mktemp "$TMP_DIR/bin/mktemp"
    /bin/ln -s /usr/bin/rm "$TMP_DIR/bin/rm"
    /bin/ln -s /usr/bin/sleep "$TMP_DIR/bin/sleep"
    /bin/ln -s /usr/bin/env "$TMP_DIR/bin/env"
    PATH="$TMP_DIR/bin"
    export PATH
    : >"$TMP_DIR/terminal.log"
    rm -f "$TMP_DIR/killall.log" "$TMP_DIR/output.log" "$TMP_DIR/wmctrl.log"
}

write_shim() {
    local name="$1"
    local body="$2"
    printf '%s\n' '#!/bin/bash' "$body" >"$TMP_DIR/bin/$name"
    /bin/chmod +x "$TMP_DIR/bin/$name"
}

assert_contains() {
    local file="$1"
    local expected="$2"
    grep -Fq "$expected" "$file"
}

assert_not_exists() {
    local file="$1"
    [ ! -e "$file" ]
}

run_test() {
    local name="$1"
    local body="$2"

    ((test_count += 1))
    reset_shims

    if eval "$body"; then
        echo "✓ Test $test_count: $name"
        ((pass_count += 1))
    else
        echo "✗ Test $test_count: $name"
        ((fail_count += 1))
    fi
}

echo "Testing launch_terminal"
echo "======================="
echo ""

run_test "healthy gnome-terminal launch passes title and does not fall back" '
    write_shim gnome-terminal '\''echo "gnome-terminal:$*" >>"'"$TMP_DIR"'/terminal.log"; exit 0'\''
    write_shim xterm '\''echo "xterm:$*" >>"'"$TMP_DIR"'/terminal.log"; exit 0'\''
    write_shim killall '\''echo "killall:$*" >>"'"$TMP_DIR"'/killall.log"; exit 0'\''

    launch_terminal "shuttle:shuttle-test" tmux attach -t "=shuttle-test" >"'"$TMP_DIR"'/output.log" 2>&1
    assert_contains "'"$TMP_DIR"'/terminal.log" "gnome-terminal:--title shuttle:shuttle-test -- tmux attach -t =shuttle-test" &&
    ! assert_contains "'"$TMP_DIR"'/terminal.log" "xterm:" &&
    assert_not_exists "'"$TMP_DIR"'/killall.log"
'

run_test "default title is \"shuttle\" when caller passes empty title" '
    write_shim gnome-terminal '\''echo "gnome-terminal:$*" >>"'"$TMP_DIR"'/terminal.log"; exit 0'\''

    launch_terminal "" tmux attach -t "=shuttle-test" >"'"$TMP_DIR"'/output.log" 2>&1
    assert_contains "'"$TMP_DIR"'/terminal.log" "gnome-terminal:--title shuttle -- tmux attach -t =shuttle-test"
'

run_test "argv launch keeps spaced attach target as one argument" '
    write_shim gnome-terminal '\''printf "%s\n" "$#" "$@" >"'"$TMP_DIR"'/terminal.log"; exit 0'\''

    launch_terminal "shuttle:spaced session" tmux attach -t "=spaced session" >"'"$TMP_DIR"'/output.log" 2>&1
    assert_contains "'"$TMP_DIR"'/terminal.log" "7" &&
    assert_contains "'"$TMP_DIR"'/terminal.log" "=spaced session"
'

run_test "D-Bus failure falls back to xterm with title flag, no killall" '
    write_shim gnome-terminal '\''echo "Failed to call CreateInstance on DBus Factory" >&2; exit 1'\''
    write_shim xterm '\''echo "xterm:$*" >>"'"$TMP_DIR"'/terminal.log"; exit 0'\''
    write_shim killall '\''echo "killall:$*" >>"'"$TMP_DIR"'/killall.log"; exit 0'\''

    launch_terminal "shuttle:shuttle-test" tmux attach -t "=shuttle-test" >"'"$TMP_DIR"'/output.log" 2>&1
    assert_contains "'"$TMP_DIR"'/terminal.log" "xterm:-T shuttle:shuttle-test -e tmux attach -t =shuttle-test" &&
    assert_not_exists "'"$TMP_DIR"'/killall.log" &&
    [ ! -s "'"$TMP_DIR"'/output.log" ]
'

run_test "D-Bus failure without xterm reports manual recovery and returns nonzero" '
    write_shim gnome-terminal '\''echo "Failed to call CreateInstance on DBus Factory" >&2; exit 1'\''
    write_shim killall '\''echo "killall:$*" >>"'"$TMP_DIR"'/killall.log"; exit 0'\''

    rm -f "'"$TMP_DIR"'/bin/xterm"
    set +e
    launch_terminal "shuttle:shuttle-test" tmux attach -t "=shuttle-test" >"'"$TMP_DIR"'/output.log" 2>&1
    rc=$?
    set -e

    [ "$rc" -ne 0 ] &&
    assert_contains "'"$TMP_DIR"'/output.log" "Terminal launch failed and xterm fallback unavailable" &&
    assert_contains "'"$TMP_DIR"'/output.log" "GNOME_TERMINAL_SCREEN" &&
    assert_not_exists "'"$TMP_DIR"'/killall.log"
'

run_test "window verification flags missing window when wmctrl is present" '
    write_shim gnome-terminal '\''echo "gnome-terminal:$*" >>"'"$TMP_DIR"'/terminal.log"; exit 0'\''
    # wmctrl shim that never lists the target window
    write_shim wmctrl '\''echo "wmctrl:$*" >>"'"$TMP_DIR"'/wmctrl.log"; echo "0x01 0 host other-window"; exit 0'\''

    set +e
    launch_terminal "shuttle:shuttle-test" tmux attach -t "=shuttle-test" >"'"$TMP_DIR"'/output.log" 2>&1
    rc=$?
    set -e

    [ "$rc" -ne 0 ] &&
    assert_contains "'"$TMP_DIR"'/output.log" "no window titled" &&
    assert_contains "'"$TMP_DIR"'/output.log" "shuttle doctor"
'

run_test "window verification passes when wmctrl finds the title" '
    write_shim gnome-terminal '\''echo "gnome-terminal:$*" >>"'"$TMP_DIR"'/terminal.log"; exit 0'\''
    write_shim wmctrl '\''echo "0x01 0 host shuttle:shuttle-test"; exit 0'\''

    set +e
    launch_terminal "shuttle:shuttle-test" tmux attach -t "=shuttle-test" >"'"$TMP_DIR"'/output.log" 2>&1
    rc=$?
    set -e

    [ "$rc" -eq 0 ] &&
    [ ! -s "'"$TMP_DIR"'/output.log" ]
'

run_test "SHUTTLE_SKIP_WINDOW_CHECK skips verification" '
    write_shim gnome-terminal '\''echo "gnome-terminal:$*" >>"'"$TMP_DIR"'/terminal.log"; exit 0'\''
    write_shim wmctrl '\''echo "0x01 0 host other-window"; exit 0'\''

    set +e
    SHUTTLE_SKIP_WINDOW_CHECK=1 launch_terminal "shuttle:shuttle-test" tmux attach -t "=shuttle-test" >"'"$TMP_DIR"'/output.log" 2>&1
    rc=$?
    set -e

    [ "$rc" -eq 0 ]
'

run_test "GNOME_TERMINAL_SCREEN is stripped from gnome-terminal env" '
    # Shim records the value (or empty) of GNOME_TERMINAL_SCREEN it sees
    write_shim gnome-terminal '\''echo "screen=${GNOME_TERMINAL_SCREEN:-EMPTY} service=${GNOME_TERMINAL_SERVICE:-EMPTY}" >>"'"$TMP_DIR"'/terminal.log"; exit 0'\''

    set +e
    GNOME_TERMINAL_SCREEN=/org/gnome/Terminal/screen/stale_handle \
    GNOME_TERMINAL_SERVICE=:1.99 \
    SHUTTLE_SKIP_WINDOW_CHECK=1 \
    launch_terminal "shuttle:shuttle-test" tmux attach -t "=shuttle-test" >"'"$TMP_DIR"'/output.log" 2>&1
    rc=$?
    set -e

    [ "$rc" -eq 0 ] &&
    assert_contains "'"$TMP_DIR"'/terminal.log" "screen=EMPTY service=EMPTY"
'

echo ""
echo "======================="
echo "Results: $pass_count/$test_count passed"

if [ "$fail_count" -eq 0 ]; then
    echo "All tests passed!"
    exit 0
else
    echo "$fail_count tests failed"
    exit 1
fi
