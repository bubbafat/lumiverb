#!/bin/bash
# Replace the catch-all scram/md5 line with trust for all connections.
# This runs once during initdb via docker-entrypoint-initdb.d.
sed -i '/^host.*all.*all.*all/d' /var/lib/postgresql/data/pg_hba.conf
echo "host all all all trust" >> /var/lib/postgresql/data/pg_hba.conf