# Load Surge I&W credentials from a local .env file into the environment.
#
#   source scripts/load-secrets.sh
#
# Intentionally has no shebang: this file is SOURCED, so it runs under whatever
# interactive shell you use (zsh on macOS by default), and the shebang would be
# ignored anyway. Everything below is therefore restricted to POSIX-compatible
# syntax that both zsh and bash accept. In particular it avoids bash's ${!name}
# indirect expansion, which zsh rejects as "bad substitution", and bash arrays.
#
# This script NEVER prints a secret value. That is not politeness — when Claude
# Code runs a command, the command's output is returned into the model's context
# and transmitted to Anthropic as part of the conversation. Credentials must live
# in the process environment and never appear in stdout, stderr, a log line, or a
# traceback.
#
# Setup:
#   cp .env.example .env
#   chmod 600 .env          # owner-only; .env is plaintext at rest
#   $EDITOR .env            # fill in values
#
# .env is gitignored, and scripts/pre-commit refuses to stage it even with -f.
#
# Verify with scripts/check-secrets.sh, which reports presence and length only
# and is the one script safe to run while an assistant is reading output.

# Deliberately no `set -e`: this file is sourced, and killing the caller's
# interactive shell over a missing optional key would be hostile.

# --- Locate the .env ---------------------------------------------------------
# BASH_SOURCE exists only in bash. In zsh a sourced file sets $0 to its own path,
# so $0 is the correct fallback. Both are overridden by SURGE_ENV_FILE.
if [ -n "${SURGE_ENV_FILE:-}" ]; then
    _surge_env_file="$SURGE_ENV_FILE"
else
    if [ -n "${BASH_SOURCE:-}" ]; then
        _surge_self="${BASH_SOURCE}"
    else
        _surge_self="$0"
    fi
    _surge_dir="$(cd "$(dirname "$_surge_self")/.." 2>/dev/null && pwd)"
    if [ -n "$_surge_dir" ] && [ -f "$_surge_dir/.env" ]; then
        _surge_env_file="$_surge_dir/.env"
    else
        _surge_env_file="./.env"     # last resort: current directory
    fi
    unset _surge_self _surge_dir
fi

if [ ! -f "$_surge_env_file" ]; then
    echo "surge: no .env found at $_surge_env_file" >&2
    echo "surge: run 'cp .env.example .env && chmod 600 .env' and fill it in" >&2
    echo "surge: or set SURGE_ENV_FILE to an explicit path" >&2
    unset _surge_env_file
    return 1 2>/dev/null || exit 1
fi

# --- Warn on loose permissions, but do not silently change the user's file ----
_surge_perms="$(stat -f '%Lp' "$_surge_env_file" 2>/dev/null || stat -c '%a' "$_surge_env_file" 2>/dev/null)"
case "$_surge_perms" in
    600|400) ;;
    "")      ;;   # stat unavailable; not worth failing over
    *) echo "surge: warning — .env is mode $_surge_perms; consider 'chmod 600 .env'" >&2 ;;
esac

# --- Load --------------------------------------------------------------------
# `set -a` exports every variable assigned while active, so .env needs no
# `export` keywords. Comments and blank lines are ignored by the shell.
set -a
. "$_surge_env_file"
set +a

# --- Report which names are still empty. Names only, never values. -----------
# Written as explicit checks rather than a loop over variable names, because
# reading a variable whose name is held in another variable requires ${!x} in
# bash and ${(P)x} in zsh, and neither is portable. Six lines beats a portability
# shim nobody will remember to maintain.
_surge_missing=""
[ -n "${APIDIRECT_API_KEY:-}" ]      || _surge_missing="$_surge_missing APIDIRECT_API_KEY"
[ -n "${FR24_API_KEY:-}" ]           || _surge_missing="$_surge_missing FR24_API_KEY"
[ -n "${STAYING_API_KEY:-}" ]        || _surge_missing="$_surge_missing STAYING_API_KEY"
[ -n "${PRICELINE_RAPIDAPI_KEY:-}" ] || _surge_missing="$_surge_missing PRICELINE_RAPIDAPI_KEY"
[ -n "${GEMINI_API_KEY:-}" ]         || _surge_missing="$_surge_missing GEMINI_API_KEY"
[ -n "${SURGE_API_TOKEN:-}" ]        || _surge_missing="$_surge_missing SURGE_API_TOKEN"

# Reported on one line rather than iterated. zsh does not word-split unquoted
# parameter expansions the way bash does, so `for x in $list` yields a single
# item under zsh and six under bash. Printing the list whole sidesteps the
# difference, and check-secrets.sh gives the per-variable breakdown anyway.
if [ -n "$_surge_missing" ]; then
    echo "surge: loaded $_surge_env_file" >&2
    echo "surge: still empty —${_surge_missing}" >&2
    echo "surge: run ./scripts/check-secrets.sh for a per-variable breakdown" >&2
else
    echo "surge: all credentials loaded from $_surge_env_file" >&2
fi

unset _surge_env_file _surge_perms _surge_missing
