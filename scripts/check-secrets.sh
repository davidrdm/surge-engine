#!/usr/bin/env bash
# Report which Surge I&W credentials are present, WITHOUT revealing any value.
#
#   source scripts/load-secrets.sh && ./scripts/check-secrets.sh
#
# Prints only: variable name, SET/MISSING, and character length. Length is a
# useful sanity check (a truncated paste is a common failure) and is not itself
# sensitive. No prefix, no suffix, no hash — those all leak search space.
#
# This is the only script safe to run in a session where output is being read by
# an assistant.

# Named `rc`, not `status`: in zsh `status` is a read-only alias for $?, so
# assigning to it aborts the script. This file is portable across sh, bash and
# zsh so that it works whether it is executed via its shebang or invoked
# explicitly as `zsh scripts/check-secrets.sh`.
rc=0

# Read a variable whose NAME is held in another variable. bash spells this
# ${!name} and zsh spells it ${(P)name}; neither is portable, so `eval` is used
# instead. Safe here because every name passed in is a literal from this file,
# never external input. The value is captured but only its length is ever shown.
_value_of() {
    eval "printf '%s' \"\${$1:-}\""
}

check() {
    name="$1"
    value="$(_value_of "$name")"
    if [ -z "$value" ]; then
        printf '  %-24s MISSING\n' "$name"
        rc=1
    else
        printf '  %-24s SET       (%d chars)\n' "$name" "${#value}"
    fi
}

optional() {
    name="$1"
    value="$(_value_of "$name")"
    if [ -z "$value" ]; then
        printf '  %-24s unset     (optional)\n' "$name"
    else
        printf '  %-24s SET       (%d chars)\n' "$name" "${#value}"
    fi
}

echo "Surge I&W credential check:"
check APIDIRECT_API_KEY
check FR24_API_KEY
check STAYING_API_KEY
check PRICELINE_RAPIDAPI_KEY
check GEMINI_API_KEY
check SURGE_API_TOKEN
echo
echo "Optional:"
# Only needed while flightradar.sandbox is true.
optional FR24_SANDBOX_KEY

echo
if [ "$rc" -eq 0 ]; then
    echo "All required credentials present in the environment."
else
    echo "Some credentials are missing. Run: source scripts/load-secrets.sh"
fi
exit "$rc"
