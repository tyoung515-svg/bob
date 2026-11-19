#!/usr/bin/env bash
set -euo pipefail
if [[ $# != 3 || ! $2 =~ ^[[:xdigit:]]{64}$ ]]; then
    echo 'fetch-checked: URL, a real SHA256 (replace REPLACE_ME), and output path required' >&2
    exit 2
fi
curl --fail --location --proto '=https' --tlsv1.2 --retry 3 --output "$3" "$1"
printf '%s  %s\n' "$2" "$3" | sha256sum --check --status || {
    rm -f -- "$3"
    echo 'fetch-checked: SHA256 mismatch' >&2
    exit 1
}
