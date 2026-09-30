# NOTICE

This project packages and rearranges **official dcm4che artifacts at build time**.
The repository itself contains no dcm4che binaries.

- `dcm4che/dcm4chee-arc-psql:5.35.1-secure` (Docker Hub) is used as the base image
  and as the source of the two *secure* wars and the WildFly configuration templates.
- `dcm4chee-arc-5.35.1-mysql.zip` (SourceForge, dcm4che project) provides the
  official MySQL EAR and `create-mysql.sql`.
- `mysql-connector-j-9.3.0.jar` (Oracle, GPL-2.0-with-FOSS-exception) is downloaded
  from Maven Central at build time.

dcm4chee-arc-light is tri-licensed MPL 1.1 / GPL 2.0 or later / LGPL 2.1 or later,
© dcm4che contributors and J4Care.

`build/build-image.py` generates `build/patched/setenv.sh` locally from the
upstream image's `/setenv.sh`. The generated file is ignored by Git and is not
included in this source checkout. A bundle made after a build can include it.

This project is not affiliated with or endorsed by the dcm4che project.
