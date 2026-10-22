# About the role

The `temporal_postgres` role runs PostgreSQL 16 for Temporal on the `temporal` user's rootless Docker daemon, with a daily local dump of both databases. `meta/argument_specs.yml` documents its options. It includes `docker_rootless` and requires Ubuntu 26.04 with `systemd`. The role isn't check-safe on a fresh host.

## Database and network

The role creates `temporal_network_name`, default `temporal`, and the `temporal-postgres` container joins only that network. It doesn't publish ports, including on `loopback`. Other Temporal roles use this network and the `temporal-postgres` DNS name on port 5432. Both the `temporal` and `temporal_visibility` databases belong to the `temporal` login. By default, the connection limit is 100. Lower limits cause the role to refuse to run. Schema installation belongs to `temporal_server`.

`temporal_postgres_images` pins official PostgreSQL images by digest for majors 16 and 15. Set `temporal_postgres_major: 15` to select 15. Both majors mount `temporal_postgres_data_dir` at `/var/lib/postgresql/data`. A major change requires a fresh directory and a dump restore or a separate migration. Changing the major alone doesn't migrate data. Keep the old directory until you verify the restored databases.

The service user's home is `/var/lib/temporal`. Its included role receives `docker_rootless_user: temporal`, `docker_rootless_home: /var/lib/temporal`, `docker_rootless_subid_start: 655360`, and `docker_rootless_data_image: /var/lib/temporal-docker-data.img`. That subordinate ID range differs from the runner pool's default. `docker_rootless_data_size` retains its predecessor's default. An empty value disables the separate Docker data filesystem. When installing files under the home, the role writes them as `temporal`.

## Credentials and startup

Private inventory supplies `temporal_postgres_op_admin_password` and `temporal_postgres_op_login_password`, each a dictionary with `vault`, `item` and `field`. The role installs the password reader but never reads credentials during Ansible execution. As the operator, supply `temporal_postgres_op_environment_file`, default `/etc/temporal-postgres/op.env`, outside the home, readable by `temporal` and inaccessible to other users, with `OP_SERVICE_ACCOUNT_TOKEN` for the service account. This role neither creates nor copies that file. Both the account and vault are operator prerequisites.

At each container start, the service reads both passwords and streams them through standard input into the container's `tmpfs`. Docker receives only the administrator password file's path in its configuration. The entrypoint initializes the databases and login on fresh data. The startup script then removes the password files before reporting readiness to `systemd`. Existing data retains its database passwords when only a vault field changes. Password rotation requires an operator database change coordinated with the clients.

Passwords never enter image layers, unit contents, Docker configuration, or persistent files under the home. PostgreSQL stores database password verifiers. The container has no service-account token. Molecule installs a fixed password stub before converge and never uses real credentials.

## Local dumps and restore

`temporal-postgres-dump.timer` runs daily by default, controlled by `temporal_postgres_dump_schedule`. Its user service writes plain SQL containing both databases to `temporal_postgres_dump_dir`, default `/var/lib/temporal/dumps`. It writes a private partial file and renames it only after both dumps succeed. A successful dump deletes completed SQL files older than `temporal_postgres_dump_retention_days`, default 14. Off-host copies belong to the backup role.

For a restore, start a fresh PostgreSQL container on the same rootless daemon and network, create the `temporal` login with the intended password, and pipe the SQL dump into `psql --username postgres -d postgres --set ON_ERROR_STOP=1`. The dump creates both databases and reconnects to each. Query both before switching clients. The Molecule scenario exercises this procedure with a probe table in each database, then selects PostgreSQL 15 on a fresh data directory.

## Verification and removal

Run the default Molecule scenario with a unique `MOLECULE_RUN_ID` and a per-role `MOLECULE_EPHEMERAL_DIRECTORY`, following `mise run test-ansible`. It uses the Docker API connection and has no check step. It checks both major versions, authenticated access to both databases, connection limits, network isolation, absent host listeners, the timer, 15-day and 13-day retention fixtures, a dump restore, credential absence, and ownership under the home.

To remove the service, run `systemctl --user disable --now temporal-postgres-dump.timer temporal-postgres-dump.service temporal-postgres.service` in the `temporal` user's `systemd` session, then remove any remaining `temporal-postgres` container from that user's daemon. Data and dumps remain. Leave the shared network and daemon in place while other Temporal services use them.
