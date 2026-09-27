#!/usr/bin/env bash
# Installs the OpenWiki CLI globally into the image.
# Retries once with build tools because better-sqlite3 may need to compile on
# platforms where no prebuilt binary matches the Node ABI.
set -euo pipefail

VERSION="${OPENWIKI_VERSION:-latest}"
echo "Installing openwiki@${VERSION}"

if ! npm install --global --no-fund --no-audit "openwiki@${VERSION}"; then
  echo "npm install failed; installing build tools and retrying" >&2
  apt-get update
  apt-get install -y --no-install-recommends build-essential
  npm install --global --no-fund --no-audit "openwiki@${VERSION}"
fi

npm ls --global --depth=0 openwiki
