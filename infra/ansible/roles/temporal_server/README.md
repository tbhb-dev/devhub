# About the server role

The `temporal_server` role runs Temporal 1.32.0 as one process with the frontend, history, matching and worker services under the `temporal` user's rootless Docker daemon. `meta/argument_specs.yml` documents its options. Digests pin the server and matching administration tools.

## Prerequisites

Apply `temporal_postgres` first. It supplies the user, daemon, network, and databases. `temporal_server_postgres_host` and `temporal_server_postgres_user` default to `temporal-postgres` and `temporal`. `temporal_server_postgres_password_file` defaults to the database role's generated `/etc/temporal-host/postgres-password`. Both schemas belong to the login, which can migrate them.

Provision certificates out of band. Three `temporal_server_certificates` entries, named `ca`, `server` and `client`, identify files on the managed host. Defaults use `/etc/temporal-host/{ca.pem,server.pem,server.key,client.pem,client.key}`. Before startup, the role requires regular files owned by `temporal:temporal` with mode `0400`. It copies them into its Transport Layer Security (TLS) directory with mode `0600`. For the server certificate, authentication usage must cover both server and client. Its subject alternative name must match `temporal_server_tls_server_name`, default `temporal-server`. Use client authentication usage for the client certificate.

## Startup and upgrades

Before every server start, the matching schema tool initializes version tables if needed and applies migrations to both databases. It then verifies their versions. Only then does startup write the password-bearing configuration into the user's runtime directory. Passwords enter migration containers through standard input and stay out of Docker metadata and persistent configuration under the home.

Before an upgrade, take a database backup and change the release and both digests together. If a migration must be undone, restore the backup because downgrading binaries alone doesn't undo schema changes.

## Transport and configuration

Frontend and internode transport require certificates from the configured CA. The role installs a Docker unit drop-in selecting explicit port forwarding and publishes only `127.0.0.1:7233`. Internode ports remain inside the server container. Dynamic configuration defaults to an empty mapping and rejects history size/count and frontend/matching rate overrides. This role leaves provisioning with `temporal operator namespace create` and `temporal operator search-attribute create` out of band and doesn't install a reconciliation script, metrics listener, or certificate expiry metrics file.

## Molecule

Run the default scenario with a private run ID and ephemeral directory. Prepare generates disposable certificates and provisions them independently of the role. The scenario checks the previous minor's schemas before upgrading, then checks both final schema versions, health, process arguments, valid and rejected client certificates, internode authentication, configuration mounted at `/dynamic.yml`, isolation of `127.0.0.1`, certificate copies, and refusal of a database without migrations. Both pinned releases use history schema 1.19 and visibility schema 1.14. The upgrade exercises migration checks without increasing those versions.
