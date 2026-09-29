#!/bin/bash
# Backup of the MySQL DICOM stack: consistent dumps of pacsdb and the keycloak
# database plus a tar of data/. Usage: ./scripts/backup.sh [dir]  (default dist/backups)
# Dumps are taken live (--single-transaction) — services keep running.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="${1:-dist/backups}"
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$OUT"
umask 077

echo "== mysqldump pacsdb (archive)..."
docker compose exec -T mysql sh -c 'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines --triggers pacsdb' \
  2>/dev/null > "$OUT/pacsdb-$STAMP.sql"

echo "== mysqldump keycloak database..."
docker compose exec -T mariadb sh -c 'mariadb-dump -uroot -p"$MARIADB_ROOT_PASSWORD" --single-transaction keycloak' \
  2>/dev/null > "$OUT/keycloak-$STAMP.sql"

echo "== tar data/ (studies, LDAP, wildfly)..."
tar -czf "$OUT/data-$STAMP.tar.gz" data

echo "== tar auxiliary files (.env, certs, secrets, build without work)..."
tar -czf "$OUT/config-$STAMP.tar.gz" \
  --exclude='build/work' --exclude='build/orig' --exclude='build/ui-source*' --exclude='build/ui-check' \
  .env my.cnf compose.yaml certs secrets build initdb scripts deploy.py make-bundle.sh 2>/dev/null || true

echo ""
ls -lh "$OUT"/*-$STAMP.* | awk '{print $9, "("$5")"}'
echo "Done. The archive contains patient data and passwords — keep it encrypted, off-host."
echo "Restore a database: docker compose exec -T mysql mysql -uroot -p\"\$MYSQL_ROOT_PASSWORD\" pacsdb < pacsdb-$STAMP.sql"
