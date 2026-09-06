#!/bin/sh
set -eu

psql --set=ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=runtime_user="$SERVICEMIND_RUNTIME_USER" \
  --set=runtime_password="$SERVICEMIND_RUNTIME_PASSWORD" <<'SQL'
SELECT format(
  'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS',
  :'runtime_user', :'runtime_password'
) WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'runtime_user') \gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'runtime_user') \gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'runtime_user') \gexec
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
  current_user, :'runtime_user'
) \gexec
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO %I',
  current_user, :'runtime_user'
) \gexec
SQL
