#!/bin/sh
# First start of the compose PostgreSQL: the two databases Temporal persists to.
# auto-setup applies Temporal's own schema into them on its first start.
set -eu
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres <<SQL
CREATE DATABASE temporal;
CREATE DATABASE temporal_visibility;
SQL
