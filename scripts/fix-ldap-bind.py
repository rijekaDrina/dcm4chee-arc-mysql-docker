#!/usr/bin/env python3
"""Rewrite the realm dcm4che LDAP federation bind credential from .env.

A fresh `--import-realm` correctly substitutes ${LDAP_ROOTPASS} from the
environment. The stored credential only goes stale when the LDAP root password
is rotated after the initial import, or when a realm/database from a mismatched
setup is restored. This script rewrites the bind credential from LDAP_ROOTPASS
in .env so such deployments recover without re-importing. Safe to re-run.
"""
from pathlib import Path
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

root = Path(__file__).resolve().parents[1]
env = dict(line.strip().split("=", 1) for line in (root / ".env").read_text().splitlines() if "=" in line)
ctx = ssl.create_default_context(cafile=str(root / "certs/ca.crt"))
ctx.check_hostname = False  # Connect to loopback while validating the internal CA chain.
base = f"https://{env['PUBLIC_BIND_IP']}:8844"

def request(path, method="GET", data=None, token=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=20) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Keycloak Admin API HTTP {error.code} for {method} {path}: {error.read()[:200]}") from None

form = urllib.parse.urlencode({
    "client_id": "admin-cli",
    "username": env["KEYCLOAK_ADMIN"],
    "password": env["KEYCLOAK_ADMIN_PASSWORD"],
    "grant_type": "password",
}).encode()
token_request = urllib.request.Request(
    base + "/realms/master/protocol/openid-connect/token",
    data=form, headers={"Content-Type": "application/x-www-form-urlencoded"},
)
with urllib.request.urlopen(token_request, context=ctx, timeout=20) as response:
    token = json.loads(response.read())["access_token"]

status, components = request(
    "/admin/realms/dcm4che/components?type=org.keycloak.storage.UserStorageProvider", token=token)
ldap = next((c for c in components if c.get("providerId") == "ldap"), None)
if ldap is None:
    raise RuntimeError("LDAP provider not found for the Keycloak realm dcm4che.")
if ldap["config"].get("bindCredential") == [env["LDAP_ROOT_PASSWORD"]]:
    print("The LDAP bind password in realm dcm4che is already correct.")
else:
    ldap["config"]["bindCredential"] = [env["LDAP_ROOT_PASSWORD"]]
    status, _ = request(f"/admin/realms/dcm4che/components/{ldap['id']}", "PUT", ldap, token)
    print("Wrote the LDAP bind password into realm dcm4che.")
