#!/usr/bin/env bash
# Usage: build.sh [version] [docker build options, e.g. --build-arg UV_SHA256=...]
set -euo pipefail
root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
version=${1:-$(git -C "$root" describe --always --tags HEAD)}
if [[ $# -gt 0 ]]; then shift; fi
[[ $version =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] || { echo 'Invalid version' >&2; exit 2; }
context=$(mktemp -d)
container=
temporary=
cleanup() {
    if [[ -n $container ]]; then docker rm -f "$container" >/dev/null || true; fi
    if [[ -n $temporary ]]; then rm -f -- "$temporary"; fi
    rm -rf -- "$context"
}
trap cleanup EXIT
# The staging tree holds only allowlisted public source; make it readable by the image builder regardless
# of the caller's umask (mktemp -d is 0700). Nothing outside this fresh directory is touched.
chmod 0755 "$context"
# Ship an explicit allowlist only. The repo also holds the owner's task notes, research,
# audits and other personal material (tasks/, research/, docs/, fable_packets/, ...) that
# must never end up in an image other people install.
if [[ ${BOB_RUNTIME_WORKTREE:-0} == 1 ]]; then
    # Explicit opt-in for reviewable, uncommitted candidates; never stage or commit.
    (umask 022; python3 "$root/deploy/runtime/snapshot.py" "$root" "$context")
else
(umask 022; git -C "$root" archive --format=tar HEAD -- \
    bobclaw-core bobclaw-gateway portal-broker migrations init.sql deploy LICENSE NOTICE .secrets/bobclaw.env.example \
    ':(exclude)bobclaw-core/tests' ':(exclude)bobclaw-gateway/tests' \
    ':(exclude)bobclaw-core/tasks' ':(exclude)bobclaw-core/BOB-LEDGER-REVIEW.md' ':(exclude)*.bak-*' \
    ':(exclude)deploy/runtime/tests' \
    | tar -xf - -C "$context")
fi
[[ -f $context/deploy/runtime/Containerfile ]] || {
    echo 'HEAD has no runtime Containerfile; git archive intentionally excludes uncommitted files.' >&2
    exit 1
}
image="bob-runtime:$version"
docker build --file "$context/deploy/runtime/Containerfile" --tag "$image" "$@" \
    --build-arg "BOB_BUILD_ID=$version" \
    --build-arg "BOB_GIT_REV=$(git -C "$root" rev-parse HEAD)" \
    --build-arg "BOB_BUILD_EPOCH=${SOURCE_DATE_EPOCH:-$(date +%s)}" "$context"
mkdir -p "$root/dist"
output="$root/dist/bob-runtime-$version.tar.gz"
[[ ! -e $output && ! -e $output.sha256 ]] || { echo "Refusing to overwrite $output" >&2; exit 1; }
container=$(docker create "$image")
temporary=$(mktemp "$root/dist/.bob-runtime.XXXXXX")
docker export "$container" | gzip -n > "$temporary"
mv -- "$temporary" "$output"
temporary=
(cd "$root/dist" && sha256sum "bob-runtime-$version.tar.gz" > "bob-runtime-$version.tar.gz.sha256")
# The outer tar hash cannot be embedded in itself. Ship this sidecar with the app.
docker cp "$container:/etc/bob/runtime-version" "$root/dist/bob-runtime-$version.version.json"
python3 - "$output" <<'PY'
import hashlib, json, pathlib, sys
archive = pathlib.Path(sys.argv[1])
version = json.loads(archive.with_name(archive.name.removesuffix('.tar.gz') + '.version.json').read_text())
with archive.open('rb') as stream:
    version['archive_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
archive.with_name(archive.name.removesuffix('.tar.gz') + '.release.json').write_text(json.dumps(version) + '\n')
PY
printf '%s\n%s\n' "$output" "$output.sha256"
