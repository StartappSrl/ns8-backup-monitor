#!/bin/bash

#
# SPDX-License-Identifier: GPL-3.0-or-later
#

# Terminate on error
set -e

images=()
repobase="${REPOBASE:-ghcr.io/nethserver}"

#
# 1. App image: a normal, standalone container image that runs the IMAP
#    poller AND the web dashboard (login + MFA + reports) in a single
#    process (see app/main.py). This is the "workload" referenced by
#    org.nethserver.images on the module image below.
#
pollerimage="backup-monitor-poller"

echo "Building the app image..."
buildah bud \
    --tag "${repobase}/${pollerimage}" \
    app/

images+=("${repobase}/${pollerimage}")

#
# 2. Module image: this is what "add-module" actually installs. It ships
#    no runtime of its own (built FROM scratch): just the imageroot/
#    action scripts, systemd units, and the compiled UI. NS8 core executes
#    imageroot/actions/* directly with its own Python "agent" environment,
#    so no interpreter needs to be baked into this image.
#
reponame="backup-monitor"

container=$(buildah from scratch)

if ! buildah containers --format "{{.ContainerName}}" | grep -q nodebuilder-backup-monitor; then
    echo "Pulling NodeJS runtime..."
    buildah from --name nodebuilder-backup-monitor -v "${PWD}:/usr/src:Z" docker.io/library/node:24.21.0-slim
fi

echo "Build static UI files with node..."
buildah run \
    --workingdir=/usr/src/ui \
    --env="NODE_OPTIONS=--openssl-legacy-provider" \
    nodebuilder-backup-monitor \
    sh -c "corepack enable && yarn install && yarn build"

buildah add "${container}" imageroot /imageroot
buildah add "${container}" ui/dist /ui
buildah config --entrypoint=/ \
    --label="org.nethserver.authorizations=traefik@node:routeadm" \
    --label="org.nethserver.images=${repobase}/${pollerimage}:latest" \
    --label="org.nethserver.rootfull=0" \
    --label="org.nethserver.tcp-ports-demand=1" \
    "${container}"
buildah commit "${container}" "${repobase}/${reponame}"

images+=("${repobase}/${reponame}")

#
# Setup CI when pushing to Github.
# Warning! docker::// protocol expects lowercase letters (,,)
if [[ -n "${CI}" ]]; then
    printf "images=%s\n" "${images[*],,}" >> "${GITHUB_OUTPUT}"
else
    printf "Publish the images with:\n\n"
    for image in "${images[@],,}"; do printf "  buildah push %s docker://%s:%s\n" "${image}" "${image}" "${IMAGETAG:-latest}" ; done
    printf "\n"
fi
