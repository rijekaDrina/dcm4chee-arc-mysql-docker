# dcm4chee-arc-mysql-docker

Dockerized **dcm4chee-arc-light 5.35.1 (secure)** PACS archive backed by **MySQL 8.4 LTS**
instead of PostgreSQL — with Keycloak (OIDC), OpenLDAP, TLS everywhere, an interactive
installer, health-checked orchestration, and end-to-end smoke tests.

[![CI](https://github.com/rijekaDrina/dcm4chee-arc-mysql-docker/actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)

## Why this exists

dcm4che officially supports MySQL for the archive database (they ship `create-mysql.sql`,
MySQL entity jars and update scripts in every release), but their **prebuilt Docker images
are PostgreSQL-only**. Teams whose DBA knowledge is MySQL/MariaDB — common in hospitals —
are left with a bare-metal WildFly install and no secure Docker path.

This repository closes that gap with a fully scripted, reproducible packaging:

- `dcm4chee-arc-mysql:5.35.1-secure` image assembled **from official artifacts only**
  (the psql-secure image + the official `5.35.1-mysql` EAR distribution + MySQL
  Connector/J from Maven Central) — see [How the image is built](#how-the-image-is-built).
- The **secure** feature set of the official image: HTTPS UI, OIDC login via Keycloak,
  TLS keystores, DICOM TLS with client certificates.
- **MySQL 8.4** with PACS-oriented tuning (`my.cnf`), `utf8mb4/utf8mb4_bin`,
  `skip-log-bin`, SSD-friendly flushing, memory limits sized for small hosts.
- Health-checked `docker compose` stack: the archive starts only after LDAP, MySQL
  (schema present) and Keycloak are healthy.
- **`deploy.py`** — an interactive installer that detects and installs missing tools
  (Docker Engine, openssl, iproute) on RHEL/AlmaLinux and Debian/Ubuntu, generates
  secrets and a test CA, builds the image, brings the stack up and configures users
  and DICOM TLS.
- Live `mysqldump`-based backup script and a portable bundle builder
  (`make-bundle.sh`) for one-command installs on a new machine.

## Quick start

Requirements: a Linux host (tested on AlmaLinux 9; ~6 GiB RAM), root access, internet.
`deploy.py` installs Docker itself if it is missing.

```sh
git clone https://github.com/rijekaDrina/dcm4chee-arc-mysql-docker.git
cd dcm4chee-arc-mysql-docker
python3 deploy.py            # interactive; --dry-run to preview, --yes for defaults
```

When it finishes it prints all URLs and writes every generated password to
`secrets/INITIAL_CREDENTIALS.txt` (mode 600):

| What | Where |
| --- | --- |
| PACS UI | `https://<host>:8444/dcm4chee-arc/ui2` |
| Keycloak admin | `https://<host>:8844/admin/` |
| WildFly console | `https://<host>:9994/console/index.html` |
| DICOM C-ECHO / C-STORE | `<host>:11113`, AE title `DCM4CHEE` |
| DICOM TLS (client cert required) | `<host>:2763` |
| HL7 MLLP / TLS | `<host>:2576` / `12576` |
| DICOMweb (QIDO/STOW/WADO) | `https://<host>:8444/dcm4chee-arc/aets/DCM4CHEE/rs` |

Connect with any DICOM viewer (e.g. Horos, MicroDicom, dcm4che's `storescu`) to
`<host>:11113` with AE title `DCM4CHEE`.

## How the image is built

There is no official MySQL flavor of the secure image, and simply pointing the
datasource at MySQL is **not enough**: the EAR ships a vendor-specific entity jar whose
`persistence.xml` pins `database-product-name` and the ID-generation strategy
(sequences on PostgreSQL, `AUTO_INCREMENT` identity on MySQL).

`build/build-image.py` therefore:

1. pulls the official `dcm4che/dcm4chee-arc-psql:5.35.1-secure` image and extracts its
   EAR, `setenv.sh` and WildFly configuration templates;
2. downloads the official `dcm4chee-arc-5.35.1-mysql.zip` distribution (SourceForge)
   and takes its EAR (consistent manifest `Class-Path` for the MySQL entity jar);
3. swaps the two **secure** wars (archive + proxy) from the psql-secure EAR into the
   MySQL EAR and fixes `application.xml` / `jboss-deployment-structure.xml`;
4. patches the `dcm4chee-arc*.xml` configurations so `java:/PacsDS` uses
   `jdbc:mysql://${env.MYSQL_HOST}:${env.MYSQL_PORT}/${env.MYSQL_DB}${env.MYSQL_JDBC_PARAMS}`
   with the `com.mysql` module (Connector/J 9.3.0) and IronJacamar MySQL
   validation classes;
5. patches `setenv.sh` with `MYSQL_*` defaults and **exports** `MYSQL_JDBC_PARAMS`
   with a leading `?` — fixing an upstream bug where JDBC parameters were silently
   ignored (they were glued onto the URL without `?`; the same bug affects
   `POSTGRES_JDBC_PARAMS` in the official image — see `docs/upstream/`).

All inputs are fetched at build time, so the repository contains no third-party
binaries. `docker build` then layers the module, patched configs and the assembled
`dcm4chee-arc-ear-5.35.1-mysql-secure.ear` onto the official base image.

## Services

| Service | Image | Notes |
| --- | --- | --- |
| `mysql` | `mysql:8.4` | `pacsdb`, schema auto-applied from `initdb/create-mysql.sql`, tuned via `my.cnf` |
| `ldap` | `dcm4che/slapd-dcm4chee` | archive configuration + users, custom root password |
| `mariadb` | `mariadb:10.11` | Keycloak's database (as in the official setup) |
| `keycloak` | `dcm4che/keycloak:25.0.6` | realm `dcm4che`, HTTPS |
| `arc` | `dcm4chee-arc-mysql:5.35.1-secure` | this project's image; memory-capped, `nofile` 65536 |

Secrets live in `.env` (mode 600, generated). Nothing is published on host ports
except the HTTPS/DICOM/HL7 ports above, bound to `PUBLIC_BIND_IP`.

## deploy.py

```
python3 deploy.py [--dry-run] [--yes] [--hostname H] [--bind-ip IP]
                  [--kc-admin USER] [--reuse-certs-from DIR] [--reconfigure]
```

- installs missing prerequisites (docker-ce, compose plugin, openssl, iproute)
- first run: generates `.env` secrets, a test CA and server certificate
  (or reuses certificates from another deployment via `--reuse-certs-from`)
- builds the image, `docker compose up -d`, waits for real health (container
  healthchecks + `WFLYSRV0025` in the WildFly log)
- runs `scripts/fix-ldap-bind.py` (repairs the LDAP federation bind credential if
  the LDAP root password ever changed after realm import), then
  `configure-users.py` (creates a PACS admin, rotates all built-in passwords)
  and `apply-ldap-config.py` (UI languages, DICOM TLS ciphers, console callback)
- re-runs over an existing installation are safe: secrets, certificates and data
  are never touched; `--reconfigure` re-applies users/languages

## Useful commands

```sh
docker compose ps                    # health at a glance
docker compose logs -f arc
scripts/backup.sh                    # live SQL dumps + application data/config archives
make-bundle.sh                       # portable tar.gz for a one-command install elsewhere
```

The backup script excludes live MySQL/MariaDB data directories; restore those
databases from the SQL dumps. Files in storage and LDAP can change while the
archive is being made, so coordinate writes or use a filesystem snapshot when
you need a point-in-time backup.

## Documentation

- [`docs/upstream/`](docs/upstream/) — write-up of the upstream
  `POSTGRES_JDBC_PARAMS` export bug with reproduction and fix
  (filed as dcm4che-dockerfiles/dcm4chee-arc-psql#26).

## Status & support

Tested end-to-end on a small VM: WildFly clean boot, C-ECHO, C-STORE + C-FIND, QIDO-RS
with OIDC tokens, DICOM TLS with client certificates, live backups. Use for
testing/evaluation; for clinical use, review everything, use your organization's CA,
and set `innodb_flush_log_at_trx_commit = 1`.

Not affiliated with dcm4che. dcm4chee-arc is tri-licensed MPL 1.1 / GPL 2.0+ /
LGPL 2.1+ (© dcm4che contributors and J4Care); this repository only builds and
rearranges their official artifacts at build time and contains no dcm4che source
beyond an MPL-licensed patched `setenv.sh` (published in full, per the MPL).
The packaging scripts here are MIT-licensed — see [LICENSE](LICENSE) and
[NOTICE](NOTICE.md).

## Contributing

PRs welcome — especially version bumps (bump `VERSION`/image tags in
`build/build-image.py` and `compose.yaml`, then run the CI smoke tests) and CI
improvements. Keep scripts POSIX-ish and dependency-light (Python 3 stdlib only).
