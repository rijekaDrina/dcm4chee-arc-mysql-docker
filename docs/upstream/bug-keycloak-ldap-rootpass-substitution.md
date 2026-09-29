# Upstream bug report draft — dcm4che-dockerfiles/keycloak

**Repo:** dcm4che-dockerfiles/keycloak (Quarkus variants, e.g. 25.0.6)
**Title:** Realm import does not substitute `${LDAP_ROOTPASS}` from the environment
when a non-default LDAP password is used

## Description

`/docker-entrypoint.d/data/import/dcm4che-realm.json` templates the LDAP federation
bind credential as:

```json
"bindCredential": [ "${LDAP_ROOTPASS}" ]
```

With Keycloak 25.0.6 (Quarkus `start --import-realm`), the placeholder is **not
reliably substituted from the container environment**, even though `setenv.sh`
exports `LDAP_ROOTPASS` (default `secret`). The realm ends up with an unusable
stored credential. Symptoms:

- `GET /admin/realms/dcm4che/users` → HTTP 400 `unknown_error` (user search goes
  through the LDAP federation);
- the PACS UI cannot list users; login of LDAP-backed users fails.

The default `secret`/`secret` combination masks the bug for everyone running the
stock slapd default password, which is why it goes unnoticed.

## Workaround / fix

After the first start, write the real password into the stored component via the
Admin API (`PUT /admin/realms/dcm4che/components/{id}` with
`config.bindCredential=[<password>]`). A ready-to-use script:
`scripts/fix-ldap-bind.py` in dcm4chee-arc-mysql-docker.

Possible upstream fixes:

1. have the keycloak image entrypoint `sed` the literal value into the realm JSON
   before import, or
2. document `LDAP_ROOTPASS` substitution as unsupported and provide the Admin-API
   fix as a supported post-install step.

## Reproduction

```sh
docker network create t
docker run -d --name ldap --network t -e LDAP_ROOTPASS=test123 dcm4che/slapd-dcm4chee:2.6.13-35.1
docker run -d --name kc --network t -e KC_BOOTSTRAP_ADMIN_USERNAME=admin \
  -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin -e LDAP_ROOTPASS=test123 \
  -e KC_DB=mariadb ... dcm4che/keycloak:25.0.6
# after import: query users through the admin API -> HTTP 400
```
