# About the role

The `temporal_postgres` role runs PostgreSQL 16 for Temporal on the `temporal` user's rootless Docker daemon. `meta/argument_specs.yml` documents its options. It includes `docker_rootless` and requires Ubuntu 26.04 with `systemd`. A fresh host requires a real apply before check mode can inspect the deployment.

## Database and network

By default, the role creates `temporal_network_name` as `temporal`, and the `temporal-postgres` container joins only that network. It doesn't publish ports. Both the `temporal` and `temporal_visibility` databases belong to the `temporal` login. The connection limit defaults to 100. Lower limits cause the role to refuse to run. Schema installation belongs to `temporal_server`.

`temporal_postgres_images` pins official PostgreSQL images by digest for majors 16 and 15. Changing `temporal_postgres_major` requires a fresh or migrated `temporal_postgres_data_dir`. A change to the major alone doesn't migrate data. Both majors mount that directory at `/var/lib/postgresql/data`.

The service user's home is `/var/lib/temporal`. With subordinate IDs starting at 655360, the included daemon role uses `docker_rootless_data_size: ""`. Docker data remains on the home's filesystem. This role installs files under the home as `temporal`.

## Credentials and startup

If `/etc/temporal-host/postgres-admin-password` or `/etc/temporal-host/postgres-password` is absent, the role generates the missing file using 36 random bytes encoded as base64 with slashes, plus signs, padding, and newlines removed. The directory has mode `0700`. Both files belong to `temporal:temporal` with mode `0400`. These tasks suppress output and never retrieve passwords from an external store.

Through standard input, the start script streams both files into the container's temporary filesystem. Docker receives only the administrator password file's path in its configuration. The entrypoint initializes the databases and login on fresh data. Before reporting readiness to `systemd`, the script removes the temporary password files. Password rotation requires an operator database change coordinated with the clients because changing a file alone doesn't change an existing database password.

## Verification

The default Molecule scenario uses a private `MOLECULE_RUN_ID` and `MOLECULE_EPHEMERAL_DIRECTORY` with the Docker API connection. It verifies both supported major versions, authenticated access, connection limits, network isolation, absent host listeners, credential absence from Docker metadata and the home, file ownership, and unchanged password contents and modification times after a second converge. A dump script, service, or timer isn't part of the role's installation.
