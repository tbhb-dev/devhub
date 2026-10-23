# The UI role

The `temporal_ui` role runs the digest-pinned Temporal web UI under the `temporal` user's rootless Docker daemon. `meta/argument_specs.yml` documents its options. Write actions remain off. Only `127.0.0.1:8080` publishes HTTP. The deployment playbook manages the HTTPS tailnet service, and the operator manages its grant.

## Prerequisites and certificates

Apply the database and server roles first. They supply the user, network, and frontend. `temporal_ui_home` defaults to `/var/lib/temporal`. By default, the frontend address is `temporal-server:7233`, and `temporal_ui_tls_server_name` is `temporal-server`. Host verification stays enabled.

Provision certificates out of band at `/var/lib/temporal/.config/temporal-ui/tls/{ca.pem,certificate.pem,private-key.pem}`. The role asserts that these are regular files owned by `temporal:temporal` with mode `0400`. Require the CA to trust the frontend and the frontend to trust the client certificate. The UI mounts this directory read-only. Container user ID 0 maps to the unprivileged host user. After replacing certificates, restart `temporal-ui.service` as `temporal`. A certificate loader or token file isn't part of the role's installation.

## Verification

The default Molecule scenario imports `playbooks/temporal.yml` from converge and checks that a second converge doesn't report changes. Prepare generates disposable certificates and provisions the inputs independently of the deployment. Side effect creates the disposable `coord` test fixture and verifies rejection of an unrelated client certificate. It restores the valid client and checks isolation from a second rootless daemon. Verify checks `/api/v1/namespaces`, read-only settings, image, network, listeners, file contents, and metadata against the hand deployment specification, and frontend TCP followed by UI HTTPS tailnet commands. Until Molecule destroys its test container, the deployment remains running.

UI version and digest are independent of the server release. Checking `/api/v1/namespaces` tests compatibility with the real server in the scenario. No test uses a real credential store or tailnet.
