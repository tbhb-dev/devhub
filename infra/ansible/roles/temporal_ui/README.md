# The `temporal_ui` role

The `temporal_ui` role runs the pinned Temporal web UI under the `temporal` user's rootless Docker daemon. `meta/argument_specs.yml` documents its options. This role always turns off write actions, and the payload decoding endpoint is empty by default. Only `127.0.0.1` publishes the HTTP port. The deployment playbook manages the tailnet service, and the operator manages its grant.

## Prerequisites and certificates

Converge `temporal_postgres` and `temporal_server` first. The database role owns `temporal_network_name` (default `temporal`) and creates the `temporal` service user and daemon. This role requires that network rather than creating it. Set `temporal_ui_home` to the same home those roles use. This role assumes `/var/lib/temporal`.

This role assumes the frontend has the container DNS name `temporal-server`, port 7233 and a certificate subject alternative name of `temporal.example.com`. Override `temporal_ui_frontend_address` and `temporal_ui_tls_server_name` together if the server uses another name. Host verification stays enabled.

Private inventory supplies `temporal_ui_client_certificate_vault` and `temporal_ui_client_certificate_item`. Its item has `ca`, `certificate` and `private-key` fields containing certificates and a private key in Privacy Enhanced Mail format. The CA must trust the frontend, and the frontend must trust the client certificate. That certificate is the client identity established for issue #28. The server role consumes provisioned files through `temporal_server_certificates`. These item fields must contain the same CA and client identity as its `ca` and `client` entries. No public inventory contains a password-manager reference.

The predecessor must install the password-manager command-line tool and provision the service account token at `temporal_ui_op_token_file`, relative to the service user's home. Each service start reads the certificate into a mode 0700 runtime directory, with mode 0600 files. Neither the token nor certificate contents enter the image, container environment, or unit. This role mounts only the certificate files read-only into the container. Container user ID 0 maps to the unprivileged host user. A certificate change requires restarting `temporal-ui.service` as that user.

## Verification

The default Molecule scenario uses the run-ID platform name, per-role state supplied by `test-ansible`, and the Docker API connection. Its converge imports `playbooks/temporal.yml`, which applies the database and server before the UI. Then it checks the pinned image and network and lists `coord` through the UI API. Further checks cover `DisableWriteActions` and `Codec.Endpoint`, plus both the listener on `127.0.0.1` and failed requests to every global interface address. Its side effect uses an unrelated client CA and requires an error in transport layer security from the API that lists `coord`. Afterward it restores the valid client even if the assertion fails. It then probes frontend isolation from a second unprivileged daemon and imports the removal playbook. Final verification checks removed resources and retained data and dumps. A fresh container has no check step.

This scenario copies the certificate generator from the server scenario. Both roles use `/run/temporal-fixtures/{ca,server,client}.pem`, with keys alongside the leaf certificates. For the UI, the scenario stub maps the `client` and `wrong` items' `ca`, `certificate` and `private-key` fields onto those files. Both clients trust the valid server CA, while the unrelated CA signs `wrong`. For the database, the stub returns the same disposable password that the server reads from `/run/temporal-fixtures/password`, and the scenario doesn't access a real credential store. Both roles share the home `/var/lib/temporal`, network `temporal`, frontend address `temporal-server:7233`, and verified server name `temporal.example.com`. Through its API, the UI checks `coord`, which the real server creates along with `coord-test`.

UI version and digest are independent of the server release. For every bump, the API check for `coord` tests compatibility. The digest pins the public multi-platform `temporalio/ui:2.48.0` image.

## Removal

As `temporal`, run `systemctl --user disable --now temporal-ui.service`, then remove any remaining `temporal-ui` container with `docker rm`. Other roles manage the network, server, and databases.
