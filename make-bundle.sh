#!/bin/bash
# Build a portable bundle of this stack for a new machine.
# Usage: ./make-bundle.sh [--full]
#   default: without build/work (the 109 MB SourceForge zip) — deploy.py downloads it
#   --full : include cached build/work artifacts (Docker images still need pulling)
set -euo pipefail
cd "$(dirname "$0")"

DIST=dist
STAMP=$(date +%Y%m%d)
BUNDLE="$DIST/dicom-mysql-bundle-$STAMP.tar.gz"
FULL=0
[ "${1:-}" = "--full" ] && FULL=1

mkdir -p "$DIST"

ARGS=(
  --exclude='./data'
  --exclude='./.env'
  --exclude='./secrets'
  --exclude='./certs'
  --exclude='./client'
  --exclude='./dist'
  --exclude='./.git'
  --exclude='./testdata'
  --exclude='./build/orig'
  --exclude='./build/ui-source'
  --exclude='./build/ui-source-5.35.1.zip'
  --exclude='./build/archive-ui.war'
  --exclude='./build/ui-check'
  --exclude='./build/cyrillic-build.log'
  --exclude='__pycache__'
  --exclude='*.pyc'
)
# .env, secrets/ and certs/ are deliberately NOT packed: deploy.py creates fresh
# ones on the new machine (certificates are reused only via --reuse-certs-from).

if [ "$FULL" -eq 0 ]; then
  echo "Packing bundle (without build/work — the new machine needs internet for SourceForge/Maven)."
  ARGS+=(--exclude='./build/work')
else
  echo "Packing bundle with cached build/work artifacts (Docker images still need pulling)."
fi

tar -czf "$BUNDLE" "${ARGS[@]}" .

SHA=$(sha256sum "$BUNDLE" | cut -d' ' -f1)
SIZE=$(du -h "$BUNDLE" | cut -f1)
echo ""
echo "Done: $BUNDLE ($SIZE)"
echo "SHA256: $SHA"
echo ""
echo "On the new machine (AlmaLinux 9 + Docker):"
echo "  mkdir -p /root/dcm4chee-mysql && tar -xzf $(basename "$BUNDLE") -C /root/dcm4chee-mysql --strip-components=1"
echo "  cd /root/dcm4chee-mysql && python3 deploy.py"
