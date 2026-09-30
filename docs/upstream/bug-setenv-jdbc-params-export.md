# Upstream bug report draft: dcm4che-dockerfiles

**Repo:** dcm4che-dockerfiles/dcm4chee-arc-psql (affects all dcm4chee-arc image variants)
**Title:** `POSTGRES_JDBC_PARAMS` is never exported, so JDBC URL parameters are glued
onto the database name without `?`

## Description

`/setenv.sh` in the `dcm4chee-arc-psql` (and related) images post-processes
`POSTGRES_JDBC_PARAMS` to prepend a `?`:

```bash
POSTGRES_JDBC_PARAMS=$(echo ${POSTGRES_JDBC_PARAMS} | sed '/^$/! s/^/?/')
```

but the result is assigned to a **shell variable without `export`**. The WildFly
configuration resolves `${env.POSTGRES_JDBC_PARAMS}` from the **process environment**,
so WildFly sees the *raw* value (without the `?`), producing:

```
jdbc:postgresql://db:5432/pacsdbuseSSL=false&...
```

## Reproduction

```sh
docker run -d --name arc -e POSTGRES_JDBC_PARAMS="sslmode=require" \
  dcm4che/dcm4chee-arc-psql:5.35.1-secure ...
# server.log:
# java.sql.SQLSyntaxErrorException: Identifier name 'pacsdbsslmode=require' is too long
```

## Fix

```bash
export POSTGRES_JDBC_PARAMS=$(echo ${POSTGRES_JDBC_PARAMS} | sed '/^$/! s/^/?/')
```

(one-line change; verified working in the dcm4chee-arc-mysql-docker packaging which
carries the equivalent `export MYSQL_JDBC_PARAMS=...` patch).
