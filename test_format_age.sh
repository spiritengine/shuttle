#!/bin/bash
# Test suite for format_age function

# Source the format_age function from shuttle
format_age() {
    local seconds=$1
    if [ "$seconds" -lt 60 ]; then
        echo "${seconds}s"
    elif [ "$seconds" -lt 3600 ]; then
        echo "$((seconds / 60))m"
    elif [ "$seconds" -lt 86400 ]; then
        local hours=$((seconds / 3600))
        local mins=$(((seconds % 3600) / 60))
        if [ "$mins" -eq 0 ]; then
            echo "${hours}h"
        else
            echo "${hours}h ${mins}m"
        fi
    else
        echo "$((seconds / 86400))d"
    fi
}

# Test cases
test_count=0
pass_count=0
fail_count=0

run_test() {
    local input=$1
    local expected=$2
    local result=$(format_age "$input")

    ((test_count++))

    if [ "$result" = "$expected" ]; then
        echo "✓ Test $test_count: format_age($input) = '$result' (expected '$expected')"
        ((pass_count++))
    else
        echo "✗ Test $test_count: format_age($input) = '$result' (expected '$expected')"
        ((fail_count++))
    fi
}

echo "Testing format_age function"
echo "============================"
echo ""

# Seconds (< 60s)
run_test 0 "0s"
run_test 30 "30s"
run_test 59 "59s"

# Minutes (60s - 3599s)
run_test 60 "1m"
run_test 120 "2m"
run_test 180 "3m"
run_test 3599 "59m"

# Hours with minutes precision (3600s - 86399s)
run_test 3600 "1h"          # exactly 1 hour
run_test 3660 "1h 1m"       # 1h 1m
run_test 3720 "1h 2m"       # 1h 2m
run_test 7200 "2h"          # exactly 2 hours
run_test 7320 "2h 2m"       # 2h 2m
run_test 10800 "3h"         # exactly 3 hours
run_test 13320 "3h 42m"     # 3h 42m (example from brief)
run_test 14340 "3h 59m"     # 3h 59m (close to 4h threshold)
run_test 14400 "4h"         # exactly 4 hours (idle cutoff threshold)
run_test 14460 "4h 1m"      # 4h 1m (just over threshold)
run_test 86399 "23h 59m"    # maximum before days

# Days (>= 86400s)
run_test 86400 "1d"
run_test 172800 "2d"
run_test 259200 "3d"

echo ""
echo "============================"
echo "Results: $pass_count/$test_count passed"

if [ "$fail_count" -eq 0 ]; then
    echo "All tests passed!"
    exit 0
else
    echo "$fail_count tests failed"
    exit 1
fi
