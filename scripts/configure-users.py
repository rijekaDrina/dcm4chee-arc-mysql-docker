#!/usr/bin/env python3
"""Create an LDAP-backed PACS admin user and rotate built-in PACS users.

Adapted from /root/DICOM/scripts/configure-users.py for the MySQL stack:
public ports 8444/8844, LDAP root password from .env (no default 'secret'),
LDAP commands run through `docker compose exec` so they work per-project.
"""
from __future__ import annotations
from pathlib import Path
import json
import secrets
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request

root = Path(__file__).resolve().parents[1]
env = dict(line.strip().split("=", 1) for line in (root / ".env").read_text().splitlines() if "=" in line)
ctx = ssl.create_default_context(cafile=str(root / "certs/ca.crt"))
ctx.check_hostname = False  # Connect to loopback while validating the internal CA chain.
base = f"https://{env['PUBLIC_BIND_IP']}:8844"
# PACS administrator account, created as a clone of the built-in root user.
ADMIN = env.get("PACS_ADMIN_USER", "pacsadmin")
LDAP_BIND = ["-x", "-D", "cn=admin,dc=dcm4che,dc=org", "-w", env["LDAP_ROOT_PASSWORD"]]

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

# The stock realm permits only the internal Compose hostname. Add the public
# HTTPS callback used by browsers on the LAN/VPN, preserving existing entries.
ui_clients = request("/admin/realms/dcm4che/clients?clientId=dcm4chee-arc-ui", token=token)[1]
if len(ui_clients) != 1:
    raise RuntimeError("Keycloak UI client dcm4chee-arc-ui not found.")
ui_client = ui_clients[0]
redirect_uris = set(ui_client.get("redirectUris", []))
redirect_uris.update({
    f"https://{env['PUBLIC_HOST']}:8444/dcm4chee-arc/ui2",
    f"https://{env['PUBLIC_HOST']}:8444/dcm4chee-arc/ui2/*",
})
if redirect_uris != set(ui_client.get("redirectUris", [])):
    ui_client["redirectUris"] = sorted(redirect_uris)
    request(f"/admin/realms/dcm4che/clients/{ui_client['id']}", "PUT", ui_client, token)

# The UI stores its selected locale in Keycloak user attribute
# attributes.locale[0]. Users are LDAP-backed, so map that attribute to the
# standard inetOrgPerson preferredLanguage field or Keycloak drops the value.
ldap_providers = request(
    "/admin/realms/dcm4che/components?type=org.keycloak.storage.UserStorageProvider",
    token=token,
)[1]
ldap = next((component for component in ldap_providers if component.get("providerId") == "ldap"), None)
if ldap is None:
    raise RuntimeError("LDAP provider not found for the Keycloak realm dcm4che.")
ldap_mappers = request(f"/admin/realms/dcm4che/components?parent={ldap['id']}", token=token)[1]
if not any(mapper.get("name") == "preferred language" for mapper in ldap_mappers):
    request("/admin/realms/dcm4che/components", "POST", {
        "name": "preferred language",
        "providerId": "user-attribute-ldap-mapper",
        "providerType": "org.keycloak.storage.ldap.mappers.LDAPStorageMapper",
        "parentId": ldap["id"],
        "config": {
            "ldap.attribute": ["preferredLanguage"],
            "is.mandatory.in.ldap": ["false"],
            "read.only": ["false"],
            "always.read.value.from.ldap": ["false"],
            "user.model.attribute": ["locale"],
        },
    }, token=token)

# Keycloak 25 only exposes declared profile attributes in userProfileMetadata.
# Declare locale as a self-editable profile field so the UI's language switcher
# can persist its attributes.locale[0] value instead of silently dropping it.
profile = request("/admin/realms/dcm4che/users/profile", token=token)[1]
if not any(attribute.get("name") == "locale" for attribute in profile.get("attributes", [])):
    profile.setdefault("attributes", []).append({
        "name": "locale",
        "displayName": "Jezik interfejsa",
        "permissions": {"view": ["admin", "user"], "edit": ["admin", "user"]},
        "multivalued": False,
    })
    request("/admin/realms/dcm4che/users/profile", "PUT", profile, token)

passwords = {name: secrets.token_urlsafe(36) for name in ("root", "admin", "user", ADMIN)}
users = request(f"/admin/realms/dcm4che/users?username={ADMIN}&exact=true", token=token)[1]
if not users:
    request("/admin/realms/dcm4che/users", "POST", {
        "username": ADMIN,
        "enabled": True,
        "firstName": ADMIN.capitalize(),
        "credentials": [{"type": "password", "value": passwords[ADMIN], "temporary": False}],
    }, token)
    users = request(f"/admin/realms/dcm4che/users?username={ADMIN}&exact=true", token=token)[1]
if len(users) != 1:
    raise RuntimeError(f"Cannot unambiguously find user {ADMIN}.")
admin_id = users[0]["id"]
if users[0].get("federationLink") is None:
    raise RuntimeError(f"User {ADMIN} is not part of the LDAP federation.")

root_users = request("/admin/realms/dcm4che/users?username=root&exact=true", token=token)[1]
if len(root_users) != 1:
    raise RuntimeError("Cannot find the initial PACS administrator.")
root_id = root_users[0]["id"]

root_realm_roles = request(f"/admin/realms/dcm4che/users/{root_id}/role-mappings/realm", token=token)[1]
request(f"/admin/realms/dcm4che/users/{admin_id}/role-mappings/realm", "POST", root_realm_roles, token)
clients = request("/admin/realms/dcm4che/clients?clientId=realm-management", token=token)[1]
if len(clients) != 1:
    raise RuntimeError("Keycloak realm-management client not found.")
client_id = clients[0]["id"]
root_client_roles = request(
    f"/admin/realms/dcm4che/users/{root_id}/role-mappings/clients/{client_id}", token=token
)[1]
request(
    f"/admin/realms/dcm4che/users/{admin_id}/role-mappings/clients/{client_id}",
    "POST", root_client_roles, token,
)

# Keycloak JS loadUserProfile() calls /realms/dcm4che/account. Without these
# account client roles that call returns 401; the UI then treats the session
# as notSecure, omits bearer tokens and hides the user/logout menu.
account_clients = request("/admin/realms/dcm4che/clients?clientId=account", token=token)[1]
if len(account_clients) != 1:
    raise RuntimeError("Keycloak account client not found.")
account_id = account_clients[0]["id"]
account_roles = request(f"/admin/realms/dcm4che/clients/{account_id}/roles", token=token)[1]
profile_roles = [role for role in account_roles if role["name"] in ("manage-account", "view-profile")]
if len(profile_roles) != 2:
    raise RuntimeError("Required account roles for the UI user profile not found.")
request(
    f"/admin/realms/dcm4che/users/{admin_id}/role-mappings/clients/{account_id}",
    "POST", profile_roles, token,
)

for name, password in passwords.items():
    matches = request(f"/admin/realms/dcm4che/users?username={name}&exact=true", token=token)[1]
    if len(matches) != 1:
        raise RuntimeError(f"Cannot unambiguously find the PACS account {name}.")
    request(f"/admin/realms/dcm4che/users/{matches[0]['id']}/reset-password", "PUT", {
        "type": "password", "value": password, "temporary": False,
    }, token)

# Write passwords to the authoritative LDAP directory as well as the Keycloak
# credential provider, then verify both LDAP bind and OIDC token issuance.
for name, password in passwords.items():
    dn = f"uid={name},ou=users,dc=dcm4che,dc=org"
    subprocess.run(
        ["docker", "compose", "exec", "-T", "ldap", "ldappasswd", "-x", "-H", "ldap://localhost"]
        + LDAP_BIND + ["-s", password, dn],
        check=True, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["docker", "compose", "exec", "-T", "ldap", "ldapwhoami", "-x", "-H", "ldap://localhost",
         "-D", dn, "-w", password],
        check=True, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    form = urllib.parse.urlencode({
        "client_id": "dcm4chee-arc-ui", "username": name,
        "password": password, "grant_type": "password",
    }).encode()
    login = urllib.request.Request(
        base + "/realms/dcm4che/protocol/openid-connect/token",
        data=form, headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(login, context=ctx, timeout=20) as response:
        login_result = json.loads(response.read())
        if response.status != 200 or "access_token" not in login_result:
            raise RuntimeError(f"OIDC login failed for account {name}.")
    # Token issuance alone is insufficient: the browser also loads the account
    # profile before it starts attaching bearer tokens to archive API requests.
    profile_status, profile = request(
        "/realms/dcm4che/account", token=login_result["access_token"],
    )
    if profile_status != 200 or profile.get("username") != name:
        raise RuntimeError(f"UI user profile not available for account {name}.")

record = [
    "Initial credentials of this test dcm4che MySQL setup. Keep local only, mode 600.",
    "The Keycloak bootstrap admin is for the master realm.",
]
for key, value in env.items():
    record.append(f"{key}={value}")
record.extend([
    "",
    "PACS/Keycloak realm dcm4che accounts:",
    "username=root; role=root, ADMINISTRATOR, realm-management; purpose: recovery admin",
    f"password_root={passwords['root']}",
    f"username={ADMIN}; role: copy of the root account privileges",
    f"password_{ADMIN}={passwords[ADMIN]}",
    "username=admin; role: admin",
    f"password_admin={passwords['admin']}",
    "username=user; role: user",
    f"password_user={passwords['user']}",
    "",
    "All built-in PACS accounts were rotated after the initial rollout.",
])
cred = root / "secrets/INITIAL_CREDENTIALS.txt"
cred.write_text("\n".join(record) + "\n", encoding="utf-8")
cred.chmod(0o600)
print(f"Created LDAP user {ADMIN}, copied admin roles and rotated the root/admin/user/{ADMIN} passwords.")
