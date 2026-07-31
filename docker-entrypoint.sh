#!/bin/sh
# Render mounts Secret Files at /etc/secrets/<filename>, but the vendored
# Clipper reads credentials.json / cookies.txt from its own directory
# (APP_DIR = vendor/youtube-clipper) — copy them into place before starting.
# No-op locally or when a file isn't provisioned.
set -e
for f in credentials.json cookies.txt; do
  if [ -f "/etc/secrets/$f" ] && [ ! -f "/app/vendor/youtube-clipper/$f" ]; then
    cp "/etc/secrets/$f" "/app/vendor/youtube-clipper/$f"
  fi
done
exec "$@"
