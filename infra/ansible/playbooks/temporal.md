# Temporal deployment

`temporal.yml` applies `docker_rootless`, `temporal_postgres`, `temporal_server` and `temporal_ui`, then enables the frontend tailnet unit before the UI tailnet unit. Use the public alias inventory together with the private inventory and limit deployment to `edge-1`. A check run against an existing deployment reads the installed files without changing them. Fresh installations require a real apply.

Docker data for the Temporal user stays under `/var/lib/temporal/.local/share/docker` with `docker_rootless_data_size: ""`, and the deployment doesn't create a separate data image or mount unit. The database role manages the shared `temporal_network_name`, default `temporal`, and creates the `temporal` and `temporal_visibility` databases. Database, server, and UI containers use the same unprivileged daemon.

## Credentials and certificates

The database role generates `/etc/temporal-host/postgres-admin-password` and `/etc/temporal-host/postgres-password` only if absent. Each uses 36 random bytes encoded as base64 with slashes, plus signs, padding, and newlines removed. Both files belong to `temporal` with mode `0400`. Password tasks suppress output. Startup streams their contents into the PostgreSQL container's temporary filesystem and deletes the temporary copies after initialization. For schema migrations and its runtime configuration, the server reads the login password.

Provision certificates out of band. Before starting the server, the playbook asserts that `/etc/temporal-host/{ca.pem,server.pem,server.key,client.pem,client.key}` and `/var/lib/temporal/.config/temporal-ui/tls/{ca.pem,certificate.pem,private-key.pem}` are regular files owned by `temporal:temporal` with mode `0400`. It never creates or fetches these certificates. Using mode `0600`, the server copies its five inputs into its configuration directory. The UI mounts its inputs directly. Certificate issuance and rotation belong to the operator.

## Endpoints and inventory

Private inventory supplies distinct `temporal_frontend_service` and `temporal_ui_service` names beginning with `svc:`. The frontend unit passes TCP traffic through to `127.0.0.1:7233`, preserving mutual Transport Layer Security (TLS). The UI unit uses HTTPS on port 443 to proxy `http://127.0.0.1:8080`. It orders itself after the frontend unit. The operator supplies `tailscaled.service`, Service approvals, tags, and grants, while this deployment doesn't manage tailnet policy.

`temporal_server_tls_server_name` and `temporal_ui_tls_server_name` remain inventory options and default to `temporal-server`. They must match the server certificate. Images remain pinned by digest in their roles. Only the frontend and UI publish host ports, both bound to `127.0.0.1`. Provision Temporal outside this playbook with `temporal operator namespace create` and `temporal operator search-attribute create`. This deployment has no dump timer, metrics listener, certificate metrics file, or removal playbook.

## Verification

Run `mise run lint-ansible` for static checks and `mise run test-tools` for the offline specification comparisons and listener tests. Run each Temporal role's Molecule scenario separately with a private run ID and ephemeral directory. Prepare generates disposable certificate authorities and provisions the certificate inputs independently of the roles. The scenarios check generated passwords, unchanged credentials on a second converge, schema upgrades, mutual TLS, read-only UI settings, file contents and metadata, ordered tailnet commands, and isolation from a second unprivileged Docker daemon. The `tailscale` recorder proves the command lines. Real tailnet exposure requires separate verification by the operator.
