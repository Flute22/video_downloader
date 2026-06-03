#!/usr/bin/env bash

# Install Python dependencies
pip install -r requirements-server.txt

# Install system packages (may fail on read-only filesystem, that's OK)
apt-get update && apt-get install -y ffmpeg nodejs npm curl unzip || echo "apt-get skipped"

# Install Deno JS runtime (needed for YouTube n-challenge solving)
curl -fsSL https://deno.land/install.sh | sh || echo "Deno install skipped"

