#!/bin/bash
# Backup of the MySQL DICOM stack: consistent dumps of pacsdb and the keycloak
# database plus a tar of application data. Usage: ./scripts/backup.sh [dir]
# Dumps are taken live (--single-transaction); filesystem files can change while
# tar runs, so coordinate writes or use a filesystem snapshot for a point-in-time backup.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="${1:-dist/backups}"
STAMP=$(date +%Y%m%d-%H%M%S)
umask 077
mkdir -p "$OUT"
TMP=$(mktemp -d "$OUT/.backup-$STAMP.XXXXXX")
trap 'rm -rf -- "$TMP"' EXIT

echo "== mysqldump pacsdb (archive)..."
docker compose exec -T mysql sh -c 'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines --triggers pacsdb' \
  > "$TMP/pacsdb-$STAMP.sql"

echo "== mysqldump keycloak database..."
docker compose exec -T mariadb sh -c 'mariadb-dump -uroot -p"$MARIADB_ROOT_PASSWORD" --single-transaction keycloak' \
  > "$TMP/keycloak-$STAMP.sql"

echo "== tar application data (studies, LDAP, WildFly)..."
# Database files captured live are not restorable snapshots; use the SQL dumps.
tar -czf "$TMP/data-$STAMP.tar.gz" --exclude='data/mysql' --exclude='data/mariadb' data

echo "== tar auxiliary files (.env, certs, secrets, build without work)..."
tar -czf "$TMP/config-$STAMP.tar.gz" \
  --exclude='build/work' --exclude='build/orig' --exclude='build/ui-source*' --exclude='build/ui-check' \
  .env my.cnf compose.yaml certs secrets build initdb scripts deploy.py make-bundle.sh

mv -- "$TMP"/* "$OUT"/

echo ""
ls -lh "$OUT"/*-$STAMP.* | awk '{print $9, "("$5")"}'
echo "Done. The archive contains patient data and passwords — keep it encrypted, off-host."
echo "Restore a database: docker compose exec -T mysql mysql -uroot -p\"\$MYSQL_ROOT_PASSWORD\" pacsdb < pacsdb-$STAMP.sql"
