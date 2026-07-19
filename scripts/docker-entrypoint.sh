#!/bin/sh
set -eu

# Bind-mounted directories retain host ownership. Fix only the two explicitly
# writable application paths, then permanently drop privileges.
chown -R bot:bot /app/data /app/logs
exec gosu bot "$@"
