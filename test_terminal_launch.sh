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
    PATH="$TMP_DIR/bin"
    export PATH
    : >"$TMP_DIR/terminal.log"
    rm -f "$TMP_DIR/killall.log" "$TMP_DIR/output.log"
    SHUTTLE_TERMINAL="gnome-terminal --"
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

run_test "healthy gnome-terminal launch does not fall back" '
    write_shim gnome-terminal '\''echo "gnome-terminal:$*" >>"'"$TMP_DIR"'/terminal.log"; exit 0'\''
    write_shim xterm '\''echo "xterm:$*" >>"'"$TMP_DIR"'/terminal.log"; exit 0'\''
    write_shim killall '\''echo "killall:$*" >>"'"$TMP_DIR"'/killall.log"; exit 0'\''

    launch_terminal "tmux attach -t shuttle-test" >"'"$TMP_DIR"'/output.log" 2>&1
    assert_contains "'"$TMP_DIR"'/terminal.log" "gnome-terminal:-- tmux attach -t shuttle-test" &&
    ! assert_contains "'"$TMP_DIR"'/terminal.log" "xterm:" &&
    assert_not_exists "'"$TMP_DIR"'/killall.log"
'

run_test "D-Bus failure falls back to xterm without killall" '
    write_shim gnome-terminal '\''echo "Failed to call CreateInstance on DBus Factory" >&2; exit 1'\''
    write_shim xterm '\''echo "xterm:$*" >>"'"$TMP_DIR"'/terminal.log"; exit 0'\''
    write_shim killall '\''echo "killall:$*" >>"'"$TMP_DIR"'/killall.log"; exit 0'\''

    launch_terminal "tmux attach -t shuttle-test" >"'"$TMP_DIR"'/output.log" 2>&1
    assert_contains "'"$TMP_DIR"'/terminal.log" "xterm:-e tmux attach -t shuttle-test" &&
    assert_not_exists "'"$TMP_DIR"'/killall.log" &&
    [ ! -s "'"$TMP_DIR"'/output.log" ]
'

run_test "D-Bus failure without xterm reports manual recovery and returns nonzero" '
    write_shim gnome-terminal '\''echo "Failed to call CreateInstance on DBus Factory" >&2; exit 1'\''
    write_shim killall '\''echo "killall:$*" >>"'"$TMP_DIR"'/killall.log"; exit 0'\''

    rm -f "'"$TMP_DIR"'/bin/xterm"
    set +e
    launch_terminal "tmux attach -t shuttle-test" >"'"$TMP_DIR"'/output.log" 2>&1
    rc=$?
    set -e

    [ "$rc" -ne 0 ] &&
    assert_contains "'"$TMP_DIR"'/output.log" "gnome-terminal-server appears to be in a bad state" &&
    assert_contains "'"$TMP_DIR"'/output.log" "will not restart it automatically" &&
    assert_not_exists "'"$TMP_DIR"'/killall.log"
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
