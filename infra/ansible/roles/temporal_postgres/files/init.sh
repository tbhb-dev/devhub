#!/usr/bin/env bash
set -euo pipefail
export TEMPORAL_LOGIN_PASSWORD
TEMPORAL_LOGIN_PASSWORD=$(cat /run/secrets/login-password)
psql --username postgres --dbname postgres --no-psqlrc --set ON_ERROR_STOP=1 <<'SQL'
\getenv login_password TEMPORAL_LOGIN_PASSWORD
CREATE ROLE temporal LOGIN PASSWORD :'login_password';
CREATE DATABASE temporal OWNER temporal;
CREATE DATABASE temporal_visibility OWNER temporal;
SQL
unset TEMPORAL_LOGIN_PASSWORD
