#!/usr/bin/env python3
"""deploy.py — interactive installer for the DCM4CHEE PACS stack on MySQL.

Brings up dcm4chee-arc 5.35.1-secure + Keycloak 25 + LDAP + MariaDB + MySQL 8.4.
All you need on a new machine is this project and Docker (deploy.py installs
Docker itself when missing):

    python3 deploy.py            # interactive, defaults offered
    python3 deploy.py --dry-run  # show the plan only
    python3 deploy.py --yes      # unattended, all defaults

Steps: environment checks -> secrets and certificates (--reuse-certs-from to reuse
existing ones, otherwise a fresh test CA) -> UI war -> image build ->
docker compose up -> wait for health -> LDAP bind fix -> users -> DICOM TLS ->
ARC restart -> report with URLs and credential locations.

Re-running over an existing installation (.env present) never touches secrets,
data or certificates: it rebuilds the image, brings the stack up and re-applies
the LDAP bind fix. Re-applying users requires --reconfigure (configure-users.py
rotates ALL PACS passwords on every run!).
"""
from __future__ import annotations

from pathlib import Path
import argparse
import os
import secrets as pysecrets
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
CERTS = ROOT / "certs"
SECRETS_DIR = ROOT / "secrets"
STACK_PORTS = (8444, 8844, 9994, 11113, 2763, 2576, 12576)


def sh(*cmd, cwd=ROOT, check=True, capture=True):
    result = subprocess.run(cmd, cwd=cwd, check=False, capture_output=capture, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(cmd)}\n{result.stdout}\n{result.stderr}")
    return result


def hr(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 62 - len(title)))


def ask(prompt: str, default: str) -> str:
    try:
        answer = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def ask_yes_no(prompt: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        try:
            answer = input(f"{prompt} [{hint}]: ").strip().lower()
        except EOFError:
            answer = ""
        if not answer:
            return default
        if answer in ("da", "d", "yes", "y"):
            return True
        if answer in ("ne", "n", "no"):
            return False
        print("Please answer 'yes' or 'no'.")


def detect_bind_ip() -> str:
    result = sh("ip", "-4", "-o", "addr", "show", "scope", "global")
    for line in result.stdout.splitlines():
        fields = line.split()
        interface = fields[1].split("@", 1)[0]
        if not interface.startswith(("docker", "br-", "veth", "zt", "tun", "wg")):
            return fields[3].split("/", 1)[0]
    return "127.0.0.1"


def ports_free(bind_ip: str) -> list[int]:
    busy = []
    for port in STACK_PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((bind_ip, port))
            except OSError:
                busy.append(port)
    return busy


def https_reachable(host_ip: str, port: int) -> bool:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        request = urllib.request.Request(f"https://{host_ip}:{port}/", method="HEAD")
        with urllib.request.urlopen(request, context=ctx, timeout=5):
            return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


def wait_container_healthy(name: str, timeout: int = 420) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = sh("docker", "inspect", "--format", "{{.State.Health.Status}}", name, check=False)
        status = result.stdout.strip()
        if status == "healthy":
            return True
        if status == "unhealthy":
            return False
        time.sleep(5)
    return False


def wait_arc_started(bind_ip: str, timeout: int = 600) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        log = sh("docker", "exec", "dcm4chee-mysql-arc-1", "sh", "-c",
                 "grep -c 'WFLYSRV0025' /opt/wildfly/standalone/log/server.log 2>/dev/null",
                 check=False).stdout.strip()
        if log.isdigit() and int(log) > 0 and https_reachable(bind_ip, 8444):
            return True
        time.sleep(10)
    return False


def write_env(values: dict[str, str]) -> None:
    ENV_PATH.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    ENV_PATH.chmod(0o600)


def reuse_source_certs(source: Path) -> str | None:
    """Copy certs from another stack installation; returns its TLS keystore password."""
    needed = ("ca.crt", "ca.p12", "arc.p12", "keycloak.p12",
              "test-client.p12", "test-client.crt", "test-client.key")
    for name in needed:
        shutil.copy2(source / "certs" / name, CERTS / name)
    for path in CERTS.glob("*.p12"):
        path.chmod(0o644)
    (CERTS / "ca.crt").chmod(0o600)
    client = ROOT / "client"
    client.mkdir(exist_ok=True)
    shutil.copy2(source / "certs" / "ca.crt", client / "test-ca.crt")
    for line in (source / ".env").read_text().splitlines():
        if line.startswith("TLS_KEYSTORE_PASSWORD="):
            return line.split("=", 1)[1]
    return None


def generate_certs(hostname: str, tls_pass: str) -> None:
    """Fresh test CA + server certificate."""

    def run(*cmd: str) -> None:
        sh(*cmd, cwd=CERTS)

    run("openssl", "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-nodes",
        "-keyout", "ca.key", "-out", "ca.crt", "-days", "3650",
        "-subj", "/CN=Test PACS CA")
    run("openssl", "req", "-new", "-newkey", "rsa:3072", "-sha256", "-nodes",
        "-keyout", "tls.key", "-out", "tls.csr", "-subj", f"/CN={hostname}",
        "-addext", f"subjectAltName=DNS:{hostname}")
    run("openssl", "x509", "-req", "-in", "tls.csr", "-CA", "ca.crt",
        "-CAkey", "ca.key", "-CAcreateserial", "-out", "tls.crt", "-days", "825",
        "-sha256", "-copy_extensions", "copy")
    for name in ("keycloak", "arc"):
        run("openssl", "pkcs12", "-export", "-out", f"{name}.p12",
            "-inkey", "tls.key", "-in", "tls.crt", "-certfile", "ca.crt",
            "-name", name, "-passout", f"pass:{tls_pass}")
    # run keytool as the invoking user so the produced ca.p12 is chown-able afterwards
    sh("docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}",
       "-v", f"{CERTS}:/certs:Z", "--entrypoint", "/usr/bin/keytool",
       "dcm4che/dcm4chee-arc-psql:5.35.1-secure",
       "-importcert", "-noprompt", "-alias", "test-ca",
       "-file", "/certs/ca.crt", "-keystore", "/certs/ca.p12",
       "-storetype", "PKCS12", "-storepass", "changeit")
    for path in CERTS.iterdir():
        path.chmod(0o600)
    for path in CERTS.glob("*.p12"):
        path.chmod(0o644)
    client = ROOT / "client"
    client.mkdir(exist_ok=True)
    shutil.copy2(CERTS / "ca.crt", client / "test-ca.crt")


def ensure_ui_war() -> None:
    """Extract the stock UI war from the official image if not present yet."""
    war = ROOT / "build" / "archive-ui.war"
    if war.exists() and war.stat().st_size > 1_000_000:
        return
    war.parent.mkdir(exist_ok=True)
    sh("docker", "create", "--name", "tmp-ui-source", "dcm4che/dcm4chee-arc-psql:5.35.1-secure")
    try:
        # a created (never started) container only carries the war in /docker-entrypoint.d
        sh("docker", "cp", "tmp-ui-source:/docker-entrypoint.d/deployments/"
            "dcm4chee-arc-ui2-5.35.1-secure.war", str(war))
    finally:
        sh("docker", "rm", "tmp-ui-source")


def final_report(hostname: str, bind_ip: str) -> None:
    print(f"""
Access URLs (for colleagues: hosts entry '{bind_ip}\t{hostname}' plus the test CA):
  PACS UI:                https://{hostname}:8444/dcm4chee-arc/ui2
  Keycloak admin portal:  https://{hostname}:8844/admin/
  WildFly console:        https://{hostname}:9994/console/index.html
  DICOM, plain TLS-less:  {hostname}:11113  (AE title: DCM4CHEE)
  DICOM TLS:              {hostname}:2763   (AE title: DCM4CHEE, client certificate required)
  HL7 MLLP / TLS:         {hostname}:2576 / 12576
  DICOMweb (QIDO/WADO):   https://{hostname}:8444/dcm4chee-arc/aets/DCM4CHEE/rs/studies
Passwords: {SECRETS_DIR}/INITIAL_CREDENTIALS.txt  (root-only, mode 600)
""")


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def os_family() -> str:
    try:
        release = Path("/etc/os-release").read_text()
    except OSError:
        return "unknown"
    for line in release.splitlines():
        if line.startswith("ID="):
            distro = line.split("=", 1)[1].strip('"').lower()
            if distro in ("almalinux", "rocky", "rhel", "centos", "fedora"):
                return "rhel"
            if distro in ("ubuntu", "debian"):
                return "debian"
    return "unknown"


def ensure_prerequisites(unattended: bool) -> None:
    """Check required tools; offer (and run) installation for anything missing."""
    missing = [tool for tool in ("docker", "openssl", "ip") if not command_exists(tool)]
    if command_exists("docker"):
        probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
        if probe.returncode != 0:
            missing.append("docker-compose-plugin")
    if not missing:
        return
    print(f"Missing tools: {', '.join(missing)}")
    family = os_family()
    if family == "unknown":
        raise SystemExit("Unknown OS — install manually: docker-ce, docker-compose-plugin, openssl, iproute2.")
    if not (unattended or ask_yes_no("Install them automatically (dnf/apt)?", True)):
        raise SystemExit("Install the tools manually, then run deploy.py again.")
    if family == "rhel":
        sh("dnf", "-y", "install", "dnf-plugins-core", check=False)
        sh("dnf", "config-manager", "--add-repo",
           "https://download.docker.com/linux/centos/docker-ce.repo", check=False)
        sh("dnf", "-y", "install", "docker-ce", "docker-ce-cli", "containerd.io",
           "docker-buildx-plugin", "docker-compose-plugin", "openssl", "iproute")
    else:
        sh("apt-get", "update")
        sh("apt-get", "install", "-y", "ca-certificates", "curl", "gnupg", "openssl", "iproute2")
        keyring = Path("/etc/apt/keyrings/docker.gpg")
        keyring.parent.mkdir(parents=True, exist_ok=True)
        sh("bash", "-c",
           "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg",
           check=False)
        sh("bash", "-c",
           'echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] '
           'https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" '
           "> /etc/apt/sources.list.d/docker.list", check=False)
        sh("apt-get", "update")
        sh("apt-get", "install", "-y", "docker-ce", "docker-ce-cli", "containerd.io",
           "docker-buildx-plugin", "docker-compose-plugin")
    if command_exists("docker") and not Path("/var/run/docker.pid").exists():
        sh("systemctl", "enable", "--now", "docker", check=False)
    print("Tools installed.")


def project_published_ports() -> set[int]:
    """Host portovi koje je objavio ovaj compose projekat (re-deploy scenario)."""
    result = sh("docker", "ps", "--filter", "label=com.docker.compose.project=dcm4chee-mysql",
                "--format", "{{.Ports}}", check=False)
    ports: set[int] = set()
    for chunk in result.stdout.replace(",", " ").split():
        if "->" not in chunk:
            continue
        host_part = chunk.split("->", 1)[0]          # npr. 172.18.2.28:8444
        if ":" not in host_part:
            continue
        try:
            ports.add(int(host_part.rsplit(":", 1)[1]))
        except ValueError:
            continue
    return ports


def main() -> None:
    parser = argparse.ArgumentParser(description="Instalacija DCM4CHEE + MySQL stacka")
    parser.add_argument("--dry-run", action="store_true", help="show the plan only, do not execute")
    parser.add_argument("--yes", "-y", action="store_true", help="unattended, accept all defaults")
    parser.add_argument("--hostname", help="DNS ime hosta (default pacs.example.com)")
    parser.add_argument("--bind-ip", help="IP to bind published ports to (default: autodetected)")
    parser.add_argument("--kc-admin", default="kcadmin", help="Keycloak master administrator (default kcadmin)")
    parser.add_argument("--reuse-certs-from", metavar="DIR",
                        help="reuse TLS certificates and CA from another stack project (dir with .env and certs/)")
    parser.add_argument("--reconfigure", action="store_true",
                        help="re-apply users and DICOM TLS on an existing installation")
    args = parser.parse_args()
    unattended = args.yes or args.dry_run
    redeploy = ENV_PATH.exists()

    hr("1/7 Environment checks")
    ensure_prerequisites(unattended)
    sh("docker", "info")
    sh("docker", "compose", "version")
    bind_ip = args.bind_ip or (detect_bind_ip() if unattended else ask("Host IP address", detect_bind_ip()))
    hostname = args.hostname or ("pacs.example.com" if unattended else ask("Host DNS name", "pacs.example.com"))
    busy = ports_free(bind_ip)
    foreign_busy = sorted(set(busy) - project_published_ports()) if redeploy else busy
    if foreign_busy:
        raise SystemExit(f"ports busy on {bind_ip}: {foreign_busy} — this stack needs: {STACK_PORTS}")
    if busy:
        print(f"Ports {sorted(set(busy))} are held by this very stack (re-deploy) — fine.")
    print(f"OK: Docker is running; {hostname} ({bind_ip}); ports {STACK_PORTS} are free.")

    hr("2/7 Secrets and certificates")
    CERTS.mkdir(mode=0o700, exist_ok=True)
    SECRETS_DIR.mkdir(mode=0o700, exist_ok=True)
    if redeploy:
        print(".env exists — re-deploy: secrets, certificates and data are NOT touched.")
    elif args.dry_run:
        print("(dry-run) would create .env with fresh passwords and "
              + (f"reuse-ovao sertifikate iz {args.reuse_certs_from}." if args.reuse_certs_from
                 else "generate a new test CA."))
    else:
        values = {
            "PUBLIC_HOST": hostname,
            "PUBLIC_BIND_IP": bind_ip,
            "MYSQL_ROOT_PASSWORD": pysecrets.token_hex(32),
            "MARIADB_ROOT_PASSWORD": pysecrets.token_hex(32),
            "LDAP_ROOT_PASSWORD": pysecrets.token_hex(32),
            "KEYCLOAK_DB_PASSWORD": pysecrets.token_hex(32),
            "PACS_DB_PASSWORD": pysecrets.token_hex(32),
            "KEYCLOAK_ADMIN_USER": args.kc_admin,
            "KEYCLOAK_ADMIN_PASSWORD": pysecrets.token_hex(32),
            "TLS_KEYSTORE_PASSWORD": pysecrets.token_hex(32),
            "EXTRA_CACERTS_PASSWORD": "changeit",
            "KEYCLOAK_ADMIN": args.kc_admin,
        }
        if args.reuse_certs_from:
            tls_pass = reuse_source_certs(Path(args.reuse_certs_from))
            if tls_pass:
                values["TLS_KEYSTORE_PASSWORD"] = tls_pass
            print(f"Reused certificates from {args.reuse_certs_from}.")
        else:
            generate_certs(hostname, values["TLS_KEYSTORE_PASSWORD"])
            print("Generated a new test CA and certificates.")
        write_env(values)
        (SECRETS_DIR / "INITIAL_CREDENTIALS.txt").write_text(
            "Initial credentials of the MySQL dcm4chee stack. Keep local only, mode 600.\n"
            "PACS accounts are created/rotated by scripts/configure-users.py.\n\n"
            + "\n".join(f"{k}={v}" for k, v in values.items()) + "\n",
            encoding="utf-8")
        (SECRETS_DIR / "INITIAL_CREDENTIALS.txt").chmod(0o600)
        print(f"Wrote .env and {SECRETS_DIR}/INITIAL_CREDENTIALS.txt (mode 600).")

    hr("3/7 UI war")
    print("(dry-run) would extract the stock UI war from the official image"
          if args.dry_run else "Extracting the stock UI war from the official image...")
    if not args.dry_run:
        ensure_ui_war()

    hr("4/7 Image build")
    if args.dry_run:
        print("(dry-run) would run: python3 build/build-image.py ; docker build -t dcm4chee-arc-mysql:5.35.1-secure build")
    else:
        sh(sys.executable, "build/build-image.py")
        sh("docker", "build", "-t", "dcm4chee-arc-mysql:5.35.1-secure", "build")

    hr("5/7 Starting the stack")
    if args.dry_run:
        print("(dry-run) would run: docker compose up -d ; then wait for ldap/mariadb/mysql/keycloak healthy and WFLYSRV0025 in the ARC log")
    else:
        sh("docker", "compose", "up", "-d")
        for name, timeout in (("dcm4chee-mysql-ldap-1", 180), ("dcm4chee-mysql-mariadb-1", 180),
                              ("dcm4chee-mysql-mysql-1", 600), ("dcm4chee-mysql-keycloak-1", 600)):
            if not wait_container_healthy(name, timeout):
                raise SystemExit(f"{name} nije zdrav — pogledaj: docker compose logs")
            print(f"  {name}: healthy")
        print("Waiting for the archive to come up (WildFly WFLYSRV0025)...")
        if not wait_arc_started(bind_ip):
            raise SystemExit("Archive did not start — check: docker compose logs arc ; "
                             "docker compose exec arc tail -100 /opt/wildfly/standalone/log/server.log")

    hr("6/7 Initial configuration")
    do_users = (not redeploy) or args.reconfigure
    if args.dry_run:
        plan = ["(dry-run) would run: python3 scripts/fix-ldap-bind.py"]
        if do_users:
            plan.append("python3 scripts/configure-users.py  (rotates the root/admin/user/PACS_ADMIN_USER passwords)")
            plan.append("python3 scripts/apply-ldap-config.py ; docker compose restart arc")
        print("\n".join(plan))
    else:
        sh(sys.executable, "scripts/fix-ldap-bind.py")
        if do_users:
            sh(sys.executable, "scripts/configure-users.py")
            sh(sys.executable, "scripts/apply-ldap-config.py")
            sh("docker", "compose", "restart", "arc")
            print("Waiting for the archive to restart...")
            if not wait_arc_started(bind_ip):
                raise SystemExit("Archive did not come back after restart — check the log.")
            print("Users and DICOM TLS configured; passwords are in secrets/INITIAL_CREDENTIALS.txt.")
        else:
            print("Re-deploy: users and passwords were not touched (use --reconfigure for that).")

    hr("7/7 Done")
    final_report(hostname, bind_ip)


if __name__ == "__main__":
    main()
