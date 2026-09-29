# dcm4chee-arc 5.35.1 "secure" variant backed by MySQL instead of PostgreSQL.
#
# dcm4che publishes only psql-based docker images, so this image is assembled from:
#  - dcm4che/dcm4chee-arc-psql:5.35.1-secure  (base: WildFly 41, dcm4che modules, secure configs)
#  - dcm4chee-arc-ear-5.35.1-mysql-secure.ear (official mysql EAR from the 5.35.1 distro,
#    with the two *secure* wars swapped in; see build-image.py)
#  - com.mysql WildFly module with MySQL Connector/J 9.3.0 (supports MySQL 8.4 LTS)
#  - patched dcm4chee-arc*.xml configs: PacsDS uses jdbc:mysql://${env.MYSQL_*} and the
#    org.jboss.jca.adapters.jdbc.extensions.mysql validation classes
#  - patched setenv.sh: deploys the -mysql-secure.ear and forwards MYSQL_* env vars
FROM dcm4che/dcm4chee-arc-psql:5.35.1-secure

COPY --chown=wildfly:wildfly mysql-module/module.xml /opt/wildfly/modules/com/mysql/main/module.xml
COPY --chown=wildfly:wildfly mysql-module/mysql-connector-j-9.3.0.jar /opt/wildfly/modules/com/mysql/main/mysql-connector-j-9.3.0.jar
COPY patched/setenv.sh /setenv.sh
COPY --chown=wildfly:wildfly patched/configuration/ /docker-entrypoint.d/configuration/
COPY --chown=wildfly:wildfly patched/dcm4chee-arc-ear-5.35.1-mysql-secure.ear /docker-entrypoint.d/deployments/dcm4chee-arc-ear-5.35.1-mysql-secure.ear

RUN rm -f /docker-entrypoint.d/deployments/dcm4chee-arc-ear-5.35.1-psql-secure.ear \
    && chmod 660 /docker-entrypoint.d/configuration/*.xml \
    && chmod 644 /opt/wildfly/modules/com/mysql/main/* \
    && chmod 755 /setenv.sh \
    && grep -q "export MYSQL_JDBC_PARAMS" /setenv.sh
