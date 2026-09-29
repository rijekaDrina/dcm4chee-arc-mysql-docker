#!/usr/bin/env python3
"""Apply DICOM TLS settings and the WildFly console callback (LDAP + Keycloak)."""
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
env = dict(line.strip().split("=", 1) for line in (root / ".env").read_text().splitlines() if "=" in line)
for filename in ("enable-dicom-tls.ldif",):
    subprocess.run(
        [
            "docker", "compose", "exec", "-T", "ldap", "ldapmodify", "-x",
            "-D", "cn=admin,dc=dcm4che,dc=org", "-w", env["LDAP_ROOT_PASSWORD"], "-f", "/dev/stdin",
        ],
        cwd=root,
        input=(root / "scripts" / filename).read_bytes(),
        check=True,
    )
subprocess.run(["python3", str(root / "scripts/configure-wildfly-console.py")], cwd=root, check=True)
print("Applied DICOM TLS settings and the WildFly console callback.")
