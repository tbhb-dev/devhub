#!/usr/bin/env bash
# cspell:ignore psql dbname psqlrc getenv
set -euo pipefail
# psql quotes the value as an SQL literal; it never becomes a command argument.
export TEMPORAL_LOGIN_PASSWORD
TEMPORAL_LOGIN_PASSWORD=$(cat /run/secrets/login-password)
psql --username postgres --dbname postgres --no-psqlrc --set ON_ERROR_STOP=1 <<'SQL'
\getenv login_password TEMPORAL_LOGIN_PASSWORD
CREATE ROLE temporal LOGIN PASSWORD :'login_password';
CREATE DATABASE temporal OWNER temporal;
CREATE DATABASE temporal_visibility OWNER temporal;
SQL
unset TEMPORAL_LOGIN_PASSWORD
