#!/bin/bash
# Demonstration of idle time precision improvement

# Source the format_age function
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

# Old function for comparison
format_age_old() {
    local seconds=$1
    if [ "$seconds" -lt 60 ]; then
        echo "${seconds}s"
    elif [ "$seconds" -lt 3600 ]; then
        echo "$((seconds / 60))m"
    elif [ "$seconds" -lt 86400 ]; then
        echo "$((seconds / 3600))h"
    else
        echo "$((seconds / 86400))d"
    fi
}

echo "Idle Time Precision Improvement"
echo "================================"
echo ""
echo "Testing times around 4h idle threshold (14400s):"
echo ""
printf "%-20s %-15s %-15s\n" "Scenario" "Old Format" "New Format"
printf "%-20s %-15s %-15s\n" "--------" "----------" "----------"

# Test cases around 4h threshold
scenarios=(
    "3h 30m:12600"
    "3h 42m:13320"
    "3h 59m:14340"
    "4h exact:14400"
    "4h 1m:14460"
    "4h 30m:16200"
)

for scenario in "${scenarios[@]}"; do
    name="${scenario%%:*}"
    seconds="${scenario##*:}"
    old=$(format_age_old "$seconds")
    new=$(format_age "$seconds")
    printf "%-20s %-15s %-15s\n" "$name" "$old" "$new"
done

echo ""
echo "Benefit: Watchman/QM can now see exact time until 4h threshold"
