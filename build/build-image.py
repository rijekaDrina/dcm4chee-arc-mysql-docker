#!/usr/bin/env python3
"""Assemble dcm4chee-arc-ear-5.35.1-mysql-secure.ear and patched WildFly configs.

Strategy:
- Take the official dcm4chee-arc-ear-5.35.1-mysql.ear (MySQL entity jar, consistent
  manifest Class-Path references) as the base.
- Swap in the two *secure* wars (dcm4chee-arc-war, dcm4chee-arc-proxy) from the
  dcm4chee-arc-ear-5.35.1-psql-secure.ear shipped in the dcm4chee-arc-psql-secure
  image, and update application.xml / jboss-deployment-structure.xml accordingly.
- Patch every dcm4chee-arc*.xml configuration so PacsDS uses the com.mysql driver
  with ${env.MYSQL_*} properties, mirroring how the psql image uses ${env.POSTGRES_*}.
- Patch setenv.sh: MYSQL_* file_env defaults, MYSQL_JDBC_PARAMS '?' handling (with
  export - the upstream image omits export, so WildFly never sees the value) and
  the new EAR file name.

All inputs are fetched automatically when missing (docker image, SourceForge
distribution zip, Maven Central driver), so this script runs on a fresh machine
that only has Docker and network access.
"""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

build = Path(__file__).resolve().parent
work = build / "work"
patched = build / "patched"
work.mkdir(exist_ok=True)

BASE_IMAGE = "dcm4che/dcm4chee-arc-psql:5.35.1-secure"
VERSION = "5.35.1"
SF_ZIP = ("https://downloads.sourceforge.net/project/dcm4che/dcm4chee-arc-light5/"
          f"{VERSION}/dcm4chee-arc-{VERSION}-mysql.zip")
CONNECTOR_URL = "https://repo1.maven.org/maven2/com/mysql/mysql-connector-j/9.3.0/mysql-connector-j-9.3.0.jar"


def fetch(url: str, dest: Path, attempts: int = 6) -> None:
    """Download with retries and integrity checks; SourceForge mirrors sometimes
    cut large files mid-stream or serve an HTML page instead of the artifact."""
    if dest.exists():
        dest.unlink()
    for attempt in range(1, attempts + 1):
        expected = None
        try:
            print(f"downloading {url} (attempt {attempt}/{attempts})")
            request = urllib.request.Request(url, headers={"User-Agent": "dcm4chee-arc-mysql-docker/1.0"})
            with urllib.request.urlopen(request, timeout=300) as response, open(dest, "wb") as out:
                expected = int(response.headers.get("Content-Length", 0)) or None
                shutil.copyfileobj(response, out)
        except OSError as error:
            print(f"  interrupted ({error})")
            dest.unlink(missing_ok=True)
            if attempt == attempts:
                raise
            time.sleep(3)
            continue
        ok = dest.stat().st_size > 0
        if ok and expected is not None and dest.stat().st_size != expected:
            print(f"  truncated ({dest.stat().st_size} of {expected} bytes)")
            ok = False
        if ok and url.endswith(".zip"):
            try:
                with zipfile.ZipFile(dest) as probe:
                    probe.namelist()
            except zipfile.BadZipFile:
                print("  incomplete zip stream (mirror cut the connection)")
                ok = False
        if ok:
            return
        dest.unlink(missing_ok=True)
        if attempt == attempts:
            raise OSError(f"download failed after {attempts} attempts: {url}")
        time.sleep(3)


def from_base_image(container_name: str) -> None:
    """Copy the psql-secure EAR, setenv.sh and configuration templates from the image."""
    subprocess.run(["docker", "create", "--name", container_name, BASE_IMAGE],
                   check=True, stdout=subprocess.DEVNULL)
    try:
        subprocess.run(["docker", "cp",
                        f"{container_name}:/docker-entrypoint.d/deployments/dcm4chee-arc-ear-{VERSION}-psql-secure.ear",
                        str(work / f"dcm4chee-arc-ear-{VERSION}-psql-secure.ear")], check=True)
        subprocess.run(["docker", "cp", f"{container_name}:/setenv.sh", str(work / "setenv.sh")], check=True)
        orig = build / "orig"
        if orig.exists():
            shutil.rmtree(orig)
        orig.mkdir(parents=True)
        subprocess.run(["docker", "cp", f"{container_name}:/docker-entrypoint.d/configuration",
                        str(orig / "configuration")], check=True)
    finally:
        subprocess.run(["docker", "rm", container_name], check=True, stdout=subprocess.DEVNULL)


# --- 0. Ensure inputs -------------------------------------------------------
psql_ear = work / f"dcm4chee-arc-ear-{VERSION}-psql-secure.ear"
mysql_ear = work / f"dcm4chee-arc-ear-{VERSION}-mysql.ear"
if not (psql_ear.exists() and (work / "setenv.sh").exists() and (build / "orig" / "configuration").is_dir()):
    print("extracting the psql-secure EAR and configurations from the base image...")
    from_base_image("tmp-build-image-extract")

if not mysql_ear.exists():
    zip_path = work / f"dcm4chee-arc-{VERSION}-mysql.zip"
    if not zip_path.exists():
        fetch(SF_ZIP, zip_path)
    with zipfile.ZipFile(zip_path) as z:
        with open(mysql_ear, "wb") as f:
            f.write(z.read(f"dcm4chee-arc-{VERSION}-mysql/deploy/dcm4chee-arc-ear-{VERSION}-mysql.ear"))
        create_sql = build.parent / "initdb" / "create-mysql.sql"
        if not create_sql.exists():
            (build.parent / "initdb").mkdir(exist_ok=True)
            create_sql.write_bytes(z.read(f"dcm4chee-arc-{VERSION}-mysql/sql/mysql/create-mysql.sql"))
    print("extracted the mysql EAR and create-mysql.sql")

connector = build / "mysql-module" / "mysql-connector-j-9.3.0.jar"
connector.parent.mkdir(exist_ok=True)
if not connector.exists() or connector.stat().st_size < 1_000_000:
    fetch(CONNECTOR_URL, connector)

# --- 1. EAR surgery ---------------------------------------------------------
patched.mkdir(exist_ok=True)
OUT_EAR = patched / f"dcm4chee-arc-ear-{VERSION}-mysql-secure.ear"
SECURE_WARS = {
    f"dcm4chee-arc-war-{VERSION}-unsecure.war": f"dcm4chee-arc-war-{VERSION}-secure.war",
    f"dcm4chee-arc-proxy-{VERSION}-unsecure.war": f"dcm4chee-arc-proxy-{VERSION}-secure.war",
}

with zipfile.ZipFile(psql_ear) as psql, zipfile.ZipFile(mysql_ear) as mysql:
    with zipfile.ZipFile(OUT_EAR, "w", zipfile.ZIP_DEFLATED) as out:
        for info in mysql.infolist():
            name = info.filename
            data = mysql.read(name)
            if name in ("META-INF/application.xml", "META-INF/jboss-deployment-structure.xml"):
                data = data.replace(b"unsecure.war", b"secure.war")
            elif name in SECURE_WARS:
                new_name = SECURE_WARS[name]
                data = psql.read(new_name)
                info = zipfile.ZipInfo(new_name, date_time=info.date_time)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o664 << 16
                name = new_name
            out.writestr(info, data)
print(f"built {OUT_EAR.name} ({OUT_EAR.stat().st_size} bytes)")

# --- 2. Patch WildFly configuration XMLs ------------------------------------
DS_DRIVER = (
    '<driver name="mysql" module="com.mysql">\n'
    '                        <driver-class>com.mysql.cj.jdbc.Driver</driver-class>\n'
    '                        <xa-datasource-class>com.mysql.cj.jdbc.MysqlXADataSource</xa-datasource-class>\n'
    "                    </driver>"
)
DS_RE = re.compile(r'<datasource jndi-name="java:/PacsDS".*?</datasource>', re.S)
DRV_RE = re.compile(r'<driver name="psql" module="org\.postgresql">.*?</driver>', re.S)


def patch_datasource(text: str) -> str:
    def repl(m: re.Match) -> str:
        block = m.group(0)
        block = block.replace(
            "jdbc:postgresql://${env.POSTGRES_HOST:db}:${env.POSTGRES_PORT:5432}/${env.POSTGRES_DB:pacsdb}${env.POSTGRES_JDBC_PARAMS:}",
            "jdbc:mysql://${env.MYSQL_HOST:mysql}:${env.MYSQL_PORT:3306}/${env.MYSQL_DB:pacsdb}${env.MYSQL_JDBC_PARAMS:}",
        )
        block = block.replace("<driver>psql</driver>", "<driver>mysql</driver>")
        block = block.replace("${env.POSTGRES_USER:pacs}", "${env.MYSQL_USER:pacs}")
        block = block.replace("${env.POSTGRES_PASSWORD:pacs}", "${env.MYSQL_PASSWORD:pacs}")
        block = block.replace(
            "org.jboss.jca.adapters.jdbc.extensions.postgres.PostgreSQLValidConnectionChecker",
            "org.jboss.jca.adapters.jdbc.extensions.mysql.MySQLValidConnectionChecker",
        )
        block = block.replace(
            "org.jboss.jca.adapters.jdbc.extensions.postgres.PostgreSQLExceptionSorter",
            "org.jboss.jca.adapters.jdbc.extensions.mysql.MySQLExceptionSorter",
        )
        return block

    text, n1 = DS_RE.subn(repl, text)
    text, n2 = DRV_RE.subn(DS_DRIVER, text)
    if n1 or n2:
        print(f"  patched PacsDS datasource (n={n1}) and driver (n={n2})")
    return text


patched_cfg = patched / "configuration"
if patched_cfg.exists():
    shutil.rmtree(patched_cfg)
shutil.copytree(build / "orig" / "configuration", patched_cfg)

patched_files = 0
for xml in sorted(patched_cfg.rglob("*.xml")):
    text = xml.read_text(encoding="utf-8")
    if "java:/PacsDS" not in text:
        continue
    before = text
    text = patch_datasource(text)
    if text != before:
        xml.write_text(text, encoding="utf-8")
        patched_files += 1
print(f"patched {patched_files} configuration XML files")
leftover = [
    str(p) for p in patched_cfg.rglob("*.xml")
    if "postgres" in p.read_text(encoding="utf-8").lower()
]
if leftover:
    sys.exit(f"postgres references remain in: {leftover}")

# --- 3. Patch setenv.sh ------------------------------------------------------
setenv = (work / "setenv.sh").read_text(encoding="utf-8")
if "file_env 'MYSQL_USER' 'pacs'" not in setenv:
    assert "file_env 'POSTGRES_PASSWORD' 'pacs'" in setenv
    setenv = setenv.replace(
        "file_env 'POSTGRES_PASSWORD' 'pacs'",
        "file_env 'POSTGRES_PASSWORD' 'pacs'\n"
        "file_env 'MYSQL_USER' 'pacs'\n"
        "file_env 'MYSQL_PASSWORD' 'pacs'",
    )
if "export MYSQL_JDBC_PARAMS" not in setenv:
    assert "POSTGRES_JDBC_PARAMS" in setenv
    setenv = setenv.replace(
        "POSTGRES_JDBC_PARAMS=$(echo ${POSTGRES_JDBC_PARAMS} | sed '/^$/! s/^/?/')",
        "POSTGRES_JDBC_PARAMS=$(echo ${POSTGRES_JDBC_PARAMS} | sed '/^$/! s/^/?/')\n"
        "\n"
        "# Append '?' in the beginning of the string if MYSQL_JDBC_PARAMS value isn't empty.\n"
        "# Must be exported: the WildFly XML resolves ${env.MYSQL_JDBC_PARAMS} from the\n"
        "# process environment, so a plain shell variable assignment would be ignored.\n"
        "export MYSQL_JDBC_PARAMS=$(echo ${MYSQL_JDBC_PARAMS} | sed '/^$/! s/^/?/')",
    )
setenv = setenv.replace(f"-{VERSION}-psql-secure.ear", f"-{VERSION}-mysql-secure.ear")
(patched / "setenv.sh").write_text(setenv, encoding="utf-8")
print("patched setenv.sh")

# --- 4. MySQL driver module --------------------------------------------------
module_xml = """<?xml version="1.0" encoding="UTF-8"?>
<module xmlns="urn:jboss:module:1.1" name="com.mysql">
    <resources>
        <resource-root path="mysql-connector-j-9.3.0.jar"/>
    </resources>

    <dependencies>
        <module name="java.se" export="true"/>
        <module name="java.xml" export="true"/>
        <module name="java.xml.crypto" export="true"/>
        <module name="jdk.xml.dom" export="true"/>
        <module name="jakarta.transaction.api"/>
    </dependencies>
</module>
"""
(build / "mysql-module" / "module.xml").write_text(module_xml, encoding="utf-8")
print("mysql driver module ready")
print("next step: docker build -t dcm4chee-arc-mysql:5.35.1-secure . (from the build/ directory)")
