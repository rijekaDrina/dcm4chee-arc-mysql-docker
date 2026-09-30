# dcm4chee-arc with MySQL in Docker

This project runs dcm4chee-arc-light 5.35.1 with MySQL 8.4. It combines the
official MySQL archive distribution with the secure components from dcm4che's
PostgreSQL Docker image. The stack also runs Keycloak, OpenLDAP and a separate
MariaDB database for Keycloak.

The goal is to provide a repeatable Docker setup for teams that need the archive
database on MySQL. The upstream Docker image uses PostgreSQL, although the
upstream distribution includes a MySQL EAR and schema.

[![CI](https://github.com/rijekaDrina/dcm4chee-arc-mysql-docker/actions/workflows/ci.yml/badge.svg)](https://github.com/rijekaDrina/dcm4chee-arc-mysql-docker/actions/workflows/ci.yml)

## Before you start

- Use a Linux host with roughly 6 GiB of RAM or more. The stack was tested on
  AlmaLinux 9. The installer has package installation paths for RHEL-based
  systems, Debian and Ubuntu.
- You need Python 3, internet access for the first build, and permission to run
  Docker. If Docker, Compose, OpenSSL or `ip` is missing, `deploy.py` can install
  it, but package installation requires root privileges.
- Choose a DNS name that clients can resolve to the host's bind IP. The
  generated test certificate contains that DNS name. A client using another
  name or the raw IP will see a certificate name mismatch.
- Check that ports 8444, 8844, 9994, 11113, 2763, 2576 and 12576 are free on
  the chosen bind IP.

## Install

```sh
git clone https://github.com/rijekaDrina/dcm4chee-arc-mysql-docker.git
cd dcm4chee-arc-mysql-docker
python3 deploy.py --dry-run --hostname pacs.example.com --bind-ip 192.0.2.10
python3 deploy.py --hostname pacs.example.com --bind-ip 192.0.2.10
```

Replace the example hostname and IP with values for your host. The installer
creates `.env`, certificates and credentials, assembles the image, starts the
Compose stack and configures the PACS users. It can take several minutes on the
first run because it downloads the upstream image and MySQL distribution.

`--dry-run` prints the planned steps without creating files or starting
containers. For unattended installation, use `--yes` together with an explicit
`--hostname` and `--bind-ip`. Without those options, `--yes` uses
`pacs.example.com` and an automatically detected IP.

After installation:

```sh
docker compose ps
docker compose logs --tail=100 arc
```

Open `https://<host>:8444/dcm4chee-arc/ui2` and sign in as `pacsadmin`. Find
its password under `password_pacsadmin` in
`secrets/INITIAL_CREDENTIALS.txt`. That file also contains the Keycloak admin
password and the rotated passwords for the built-in PACS accounts. It is
created with mode 600. The generated CA certificate is `certs/ca.crt`; clients
must trust it to avoid browser certificate warnings. Resolve `<host>` through
DNS or a hosts-file entry that points to the bind IP.

### Published ports

| Port | Service | Transport |
| --- | --- | --- |
| 8444 | PACS UI and DICOMweb | HTTPS |
| 8844 | Keycloak admin and OIDC | HTTPS |
| 9994 | WildFly console | HTTPS |
| 11113 | DICOM, AE title `DCM4CHEE` | Plain TCP |
| 2763 | DICOM, AE title `DCM4CHEE` | TLS with client certificate |
| 2576 | HL7 MLLP | Plain TCP |
| 12576 | HL7 MLLP | TLS |

The DICOMweb base URL is
`https://<host>:8444/dcm4chee-arc/aets/DCM4CHEE/rs`. The archive database,
Keycloak database and LDAP ports are internal to the Compose network.
Published ports bind to `PUBLIC_BIND_IP` from `.env`.

The installer generates a test CA. The plain DICOM and HL7 ports remain
available, and the archive's MySQL connection is configured with `useSSL=false`.
Limit network access accordingly. Before use with clinical data, review the
security settings and replace the test certificates with certificates issued
by your organization.

## Running it again

Running `python3 deploy.py` in an installation with an existing `.env` rebuilds
the image and brings the stack up using the saved hostname, bind IP, passwords
and certificates. It does not rotate PACS user passwords. The hostname and bind
IP cannot be changed with command-line options on a rerun.

Use `python3 deploy.py --reconfigure` to reapply the user and LDAP settings.
This **rotates the passwords** for `root`, `admin`, `user` and the PACS admin
account. Read the new values from `secrets/INITIAL_CREDENTIALS.txt` after it
finishes.

`--reuse-certs-from DIR` is for a new installation and reads `DIR/.env` and
`DIR/certs/`. Reuse the certificates only if they cover the hostname of the new
installation. A fresh installation otherwise creates its own test CA.

Run `python3 deploy.py --help` for the full option list.

## Backup and bundle

```sh
scripts/backup.sh                  # writes to dist/backups/
scripts/backup.sh /path/to/backup  # choose another destination
make-bundle.sh                     # package the project without local data or secrets
make-bundle.sh --full              # also include cached build/work downloads
```

The backup contains SQL dumps of `pacsdb` and Keycloak's database, an archive
of application data, and an archive of configuration, certificates and secrets.
Keep the output encrypted and off the host. The script excludes live MySQL and
MariaDB data directories because their files are not restorable live copies;
restore the databases from the SQL dumps. Files in storage and LDAP can change
during the tar operation. Coordinate writes or use a filesystem snapshot if
the backup needs to represent one point in time.

The bundle is for installing on another machine. It excludes `.env`, secrets,
certificates and patient data, so the new machine creates its own credentials.
`--full` includes cached downloads from `build/work`, but Docker images still
need to be available on the target machine. This is not an offline installer.

## How the image is assembled

`build/build-image.py` takes the official MySQL EAR and SQL schema from the
dcm4chee-arc-light 5.35.1 MySQL distribution. It takes the two secure WARs,
WildFly configuration and `setenv.sh` from
`dcm4che/dcm4chee-arc-psql:5.35.1-secure`. The script then:

1. Replaces the nonsecure archive and proxy WARs in the MySQL EAR with the
   secure WARs and updates the deployment descriptors.
2. Changes the `PacsDS` datasource to use MySQL and adds Connector/J 9.3.0 as
   a WildFly module.
3. Patches `setenv.sh` to pass `MYSQL_JDBC_PARAMS` with a leading `?` to
   WildFly. The related upstream PostgreSQL issue is recorded in
   [docs/upstream/](docs/upstream/).

The repository does not store the downloaded EAR, SQL schema or driver JAR.
`docker build` adds the assembled files to the upstream image.

## Verification and limits

CI assembles the image and validates Compose on pull requests. Pushes to
`main` also deploy the stack and run C-ECHO, C-STORE and a QIDO-RS query against
a synthetic study. A manual workflow run can run the same stack test.

The default [MySQL settings](my.cnf) use
`innodb_flush_log_at_trx_commit = 2`, which can lose recent transactions after
a power failure. For clinical use, review that setting, backup and restore
procedures, access controls, certificates and network exposure with the team
responsible for the deployment.

This project is not affiliated with dcm4che. See [NOTICE.md](NOTICE.md) for
upstream components and [LICENSE](LICENSE) for this repository's code.
