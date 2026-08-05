#!/bin/sh
set -e

# Runs as root just long enough to (1) own the workspace mount and (2) match
# the docker socket's group at runtime — the socket's GID differs per host, so
# it is detected here instead of being baked in at build time (the DOCKER_GID
# build-arg dance miragen-mcp used). Then drops to the unprivileged user.

WORKSPACE="${MIRAGEN_WORKSPACE:-/opt/miragen}"
mkdir -p "${WORKSPACE}/agents"
chown -R miragend "${WORKSPACE}"

SOCK=/var/run/docker.sock
if [ -S "${SOCK}" ]; then
    SOCK_GID="$(stat -c %g "${SOCK}")"
    if ! getent group "${SOCK_GID}" >/dev/null 2>&1; then
        groupadd -g "${SOCK_GID}" docker-host
    fi
    usermod -aG "$(getent group "${SOCK_GID}" | cut -d: -f1)" miragend
else
    echo "WARNING: ${SOCK} is not mounted — miragend cannot manage containers" >&2
fi

exec gosu miragend miragend
