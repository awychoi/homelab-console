#!/bin/sh
# Scan for credential material that must never be committed or pushed.
#
#   tools/secret_scan.sh              # tracked files in the working tree
#   tools/secret_scan.sh --history    # every commit reachable from any ref
#
# Exits 0 when clean, 1 when a hit is found, 2 on usage/CLI errors.
# High-confidence patterns only, so a clean run is meaningful and a failure is
# never a test fixture.  No network access and no third-party tools required.
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$REPO"

case "${1:-}" in
    "")        MODE=worktree ;;
    --history) MODE=history ;;
    *)         echo "usage: $0 [--history]" >&2; exit 2 ;;
esac

# Private keys, provider token prefixes, and credentials embedded in a URL.
PATTERN='-----BEGIN [A-Z ]*PRIVATE KEY-----'
PATTERN="$PATTERN|(ASIA|AKIA)[0-9A-Z]{16}"
PATTERN="$PATTERN|gh[pousr]_[A-Za-z0-9]{20,}"
PATTERN="$PATTERN|github_pat_[A-Za-z0-9_]{20,}"
PATTERN="$PATTERN|glpat-[A-Za-z0-9_-]{20,}"
PATTERN="$PATTERN|xox[baprs]-[A-Za-z0-9-]{10,}"
PATTERN="$PATTERN|sk-ant-[A-Za-z0-9_-]{20,}"
PATTERN="$PATTERN|sk-[A-Za-z0-9]{32,}"
PATTERN="$PATTERN|AIza[0-9A-Za-z_-]{35}"
PATTERN="$PATTERN|tskey-[A-Za-z0-9_-]{20,}"
PATTERN="$PATTERN|AGE-SECRET-KEY-1[0-9A-Z]{10,}"
PATTERN="$PATTERN|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\."
PATTERN="$PATTERN|[a-zA-Z][a-zA-Z0-9+.-]*://[^/[:space:]@]{1,64}:[^/[:space:]@]{3,64}@"

failed=0

report() {
    # $1 = severity/kind, remaining args are the finding
    kind=$1
    shift
    echo "FAIL [$kind] $*"
    failed=1
}

# 1. A secret-named file that is tracked despite .gitignore (e.g. `git add -f`).
tracked_secret_names() {
    git ls-files -z | tr '\0' '\n' | while IFS= read -r path; do
        case "$path" in
            */.env|.env|*/.env.*|*.env)
                echo "$path" ;;
            *.pem|*.key|*.p12|*.pfx|*.jks|*.keystore|*.kdbx|*.ovpn)
                echo "$path" ;;
            */.netrc|.netrc|*/.pgpass|.pgpass|*/.htpasswd|.htpasswd|*/htpasswd)
                echo "$path" ;;
            */credentials|credentials|*/credentials.json|*/id_rsa|*/id_dsa|*/id_ecdsa|*/id_ed25519|*/id_ecdsa_sk|*/id_ed25519_sk)
                echo "$path" ;;
        esac
    done
}

# 2. Credential content. `git grep` exits 1 when nothing matches.
scan_content() {
    if git grep -n -I -E -e "$PATTERN" "$@" -- . ; then
        return 1
    fi
    return 0
}

echo "secret scan: $MODE mode ($REPO)"

for path in $(tracked_secret_names); do
    report tracked-secret-file "$path"
done

if [ "$MODE" = worktree ]; then
    if ! scan_content; then
        report credential-content "tracked files contain a credential pattern"
    fi
else
    # Every commit reachable from any ref, including deleted files.
    hits=0
    for rev in $(git rev-list --all); do
        if ! scan_content "$rev"; then
            hits=$((hits + 1))
        fi
        subject=$(git log -1 --format='%s' "$rev")
        if printf '%s' "$subject" | grep -E -q -e "$PATTERN"; then
            report commit-message "$(git log -1 --format='%h %s' "$rev")"
        fi
    done
    if [ "$hits" -gt 0 ]; then
        report credential-content "$hits commit(s) contain a credential pattern"
    fi
fi

if [ "$failed" -ne 0 ]; then
    echo "secret scan: FAILED — remove the value, rotate it, then rewrite or expire the credential."
    exit 1
fi

echo "secret scan: clean"
