#!/usr/bin/env bash

# Add Deno to PATH so yt-dlp can find it
export PATH=$PATH:/opt/render/.deno/bin

# Start the server
gunicorn app:app --bind 0.0.0.0:$PORT --timeout 300 --workers 2
