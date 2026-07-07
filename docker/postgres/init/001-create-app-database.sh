set -e

: "${OSCE_APP_DB:?OSCE_APP_DB is required}"
: "${OSCE_APP_USER:?OSCE_APP_USER is required}"
: "${OSCE_APP_PASSWORD:?OSCE_APP_PASSWORD is required}"

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=app_db="$OSCE_APP_DB" \
  --set=app_user="$OSCE_APP_USER" \
  --set=app_password="$OSCE_APP_PASSWORD" <<'EOSQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password')
WHERE NOT EXISTS (
  SELECT 1
  FROM pg_catalog.pg_roles
  WHERE rolname = :'app_user'
)\gexec

ALTER ROLE :"app_user" WITH LOGIN PASSWORD :'app_password';

SELECT format('CREATE DATABASE %I OWNER %I ENCODING %L', :'app_db', :'app_user', 'UTF8')
WHERE NOT EXISTS (
  SELECT 1
  FROM pg_catalog.pg_database
  WHERE datname = :'app_db'
)\gexec

ALTER DATABASE :"app_db" OWNER TO :"app_user";
EOSQL

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$OSCE_APP_DB" \
  --set=app_db="$OSCE_APP_DB" \
  --set=app_user="$OSCE_APP_USER" <<'EOSQL'
ALTER SCHEMA public OWNER TO :"app_user";
GRANT CONNECT ON DATABASE :"app_db" TO :"app_user";
GRANT USAGE, CREATE ON SCHEMA public TO :"app_user";
EOSQL
