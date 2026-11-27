#!/usr/bin/env bash
set -euo pipefail
umask 077
: "${DATABASE_URL:?Set DATABASE_URL}"
directory="${BACKUP_DIR:-backups}"
mkdir -p "$directory"
archive="$directory/goat-$(date -u +%Y%m%dT%H%M%SZ).dump"
url="${DATABASE_URL/postgresql+asyncpg:/postgresql:}"
PGDATABASE="$url" pg_dump --no-owner --no-acl --format=custom --file="$archive.tmp"
pg_restore --list "$archive.tmp" >/dev/null
mv "$archive.tmp" "$archive"
printf '%s\n' "$archive"
