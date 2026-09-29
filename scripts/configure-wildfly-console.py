#!/usr/bin/env python3
"""Register the public WildFly console callback without changing user passwords.

Adapted for the MySQL stack: Keycloak on :8844, WildFly console on :9994.
"""
from __future__ import annotations
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

def request(path: str, method: str = "GET", data=None, token: str | None = None):
    headers = {"Accept": "application/json"}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=20) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Keycloak Admin API HTTP {error.code} for {method} {path}") from None

form = urllib.parse.urlencode({
    "client_id": "admin-cli",
    "username": env["KEYCLOAK_ADMIN"],
    "password": env["KEYCLOAK_ADMIN_PASSWORD"],
    "grant_type": "password",
}).encode()
token_request = urllib.request.Request(
    base + "/realms/master/protocol/openid-connect/token",
    data=form,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
with urllib.request.urlopen(token_request, context=ctx, timeout=20) as response:
    token = json.loads(response.read())["access_token"]


clients = request('/admin/realms/dcm4che/clients?clientId=wildfly-console', token=token)[1]
if len(clients) != 1:
    raise RuntimeError("WildFly console client not found.")
for client in clients:
    print('CLIENT', client['clientId'], 'redirects', client.get('redirectUris'), 'origins', client.get('webOrigins'))
    client['redirectUris'] = sorted(set(client.get('redirectUris', [])) | {f"https://{env['PUBLIC_HOST']}:9994/console/*"})
    client['webOrigins'] = sorted(set(client.get('webOrigins', [])) | {f"https://{env['PUBLIC_HOST']}:9994"})
    request('/admin/realms/dcm4che/clients/' + client['id'], 'PUT', client, token)
    print('Updated public console callback.')
