#!/usr/bin/env bash
# Usage: smoke.sh bob-runtime:<version>
# Default is unprivileged. For hosts requiring it, explicitly set BOB_SMOKE_PRIVILEGED=1.
set -euo pipefail
[[ $# == 1 ]] || { echo 'Usage: smoke.sh bob-runtime:<version>' >&2; exit 2; }
name="bob-runtime-smoke-$$"
created=0
health=$(mktemp)
cleanup() {
    if [[ $created == 1 ]]; then
        docker stop --time 20 "$name" >/dev/null || true
        docker rm -f "$name" >/dev/null || true
    fi
    rm -f -- "$health"
}
trap cleanup EXIT
flags=(--name "$name" --env container=docker --tmpfs /run --tmpfs /run/lock --tmpfs /tmp --cgroupns private)
case ${BOB_SMOKE_PRIVILEGED:-0} in
    0) ;;
    1) flags+=(--privileged) ;;
    *) echo 'BOB_SMOKE_PRIVILEGED must be 0 or 1' >&2; exit 2 ;;
esac
docker create "${flags[@]}" "$1" /sbin/init >/dev/null
created=1
docker start "$name" >/dev/null
docker exec "$name" sh -c 'command -v fetch-checked'
ready=0
for ((attempt=0; attempt<120; attempt++)); do
    if docker exec "$name" test -f /var/lib/bob/.firstboot-complete; then ready=1; break; fi
    if [[ $(docker inspect --format '{{.State.Running}}' "$name") != true ]]; then break; fi
    if docker exec "$name" systemctl is-failed --quiet bob-firstboot.service; then break; fi
    sleep 2
done
if [[ $ready != 1 ]]; then
    docker logs "$name" >&2 || true
    docker exec "$name" journalctl --no-pager -u bob-firstboot.service -n 50 >&2 || true
    echo 'Firstboot did not complete. If systemd reports cgroup permissions, this host may require explicit BOB_SMOKE_PRIVILEGED=1.' >&2
    exit 1
fi
head -c 32 /dev/urandom | base64 | docker exec -i "$name" bob-lane add owner --password-stdin
for ((attempt=0; attempt<120; attempt++)); do
    if docker exec "$name" bob-health > "$health"; then
        cat "$health"
        exit 0
    fi
    sleep 2
done
cat "$health"
docker exec "$name" journalctl --no-pager -u bob-core@owner.service -u bob-gateway@owner.service -n 80 >&2 || true
exit 1
