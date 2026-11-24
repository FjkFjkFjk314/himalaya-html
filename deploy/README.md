# Local Docker Compose

Run these commands from the repository root to build and check the image.

```sh
docker compose --profile executor config --quiet
docker compose --profile executor build himalaya
docker compose run --rm himalaya --version
```

Each `docker compose run` starts a new Himalaya CLI process. The service
publishes no ports. The `himalaya-state` volume at `/var/lib/himalaya` persists
across invocations.

## Connect an account

1. Create a private directory outside the repository and export its absolute
   path as `HIMALAYA_DEPLOY_DIR`. Copy
   [the sample config](examples/config.toml) to `$HIMALAYA_DEPLOY_DIR/config.toml`
   and replace the example address and server settings.
2. Store the IMAP and SMTP credentials in
   `$HIMALAYA_DEPLOY_DIR/secrets/imap_password` and
   `$HIMALAYA_DEPLOY_DIR/secrets/smtp_password`. Each file contains one
   credential. Give the account mapped to container UID/GID `10001:10001` read
   access to the config and secret files. For rootful Linux without user
   namespace remapping, host ownership `10001:10001` and mode `0400` work.
   Compose bind-mounts file-backed secrets with the host permissions.
3. Load the overlay and list the configured accounts.

   ```sh
   docker compose -f compose.yaml -f deploy/examples/compose.override.yaml \
     --profile executor config --quiet
   docker compose -f compose.yaml -f deploy/examples/compose.override.yaml \
     run --rm himalaya -c /run/himalaya/config.toml account list
   ```

The config also shows Gmail and Microsoft Graph token commands. For those
accounts, add the corresponding secret to a copy of the Compose overlay kept
outside the repository, or provide a helper that prints a current access token
to stdout. Keep credentials, OAuth state, mail data, and any local TLS or
authentication files in the private deployment directory or in deployment
volumes. Keep trace logging off when using account credentials.

With Docker Compose 2.24.4 or later, run `python3 tests/docker/smoke.py` to build
the image and test with synthetic mail and an SMTP collector on an isolated
Docker network.
