# Seoul mail reader

The dedicated HTTPS virtual host on the Seoul edge forwards to Caddy on
`127.0.0.1:18080`. Caddy sends reader requests to the loopback reader on
`127.0.0.1:8080` only after Authelia verifies both password and TOTP.
Authelia serves its portal at `/authelia/` on the same host and keeps its
session data in a dedicated, persistent Redis instance. The reader fetches mail
through fixed-command SSH to the private executor.

## Install

1. Verify that Seoul can reach the executor over the private network and
   confirm the executor's SSH host key through a trusted channel. Generate a
   dedicated SSH key on Seoul under the `himalaya-reader` service account.
   Install its public key on the executor for the restricted, read-only reader
   command. Install the trusted host key in the reader account's `known_hosts`.
   Set the reader's SSH argv in `/srv/himalaya-mail/shared/reader.env` and start
   the reader with the service unit in this deployment package. Verify that its
   port is bound to loopback and that only the intended mailboxes are visible.
2. On Seoul, set `READER_HOST` to the dedicated FQDN and `READER_AUTH_DIR` to an
   absolute private directory outside the release and repository. Keep these
   settings in a private environment file and pass it to `docker compose` with
   `--env-file` for later invocations. If loopback ports conflict, adjust
   `READER_PROXY_PORT`, `READER_AUTH_PORT`, and `READER_PORT` as needed.
   Match `READER_PORT` in the reader environment and point the edge at
   `READER_PROXY_PORT`. Use a dedicated hostname whose DNS and certificate you
   control.
3. From `/srv/himalaya-mail/current/deploy/seoul`, use a private terminal to
   create the secrets and generate the password hash. In this example
   `READER_HOST` and `READER_AUTH_DIR` are exported in the shell.

   ```sh
   umask 077
   install -d -m 0700 "$READER_AUTH_DIR"
   for name in session storage; do
       (set -C; openssl rand -hex 64 > "$READER_AUTH_DIR/$name") || exit 1
   done
   docker compose pull
   docker compose run --rm --no-deps --entrypoint authelia authelia \
       crypto hash generate argon2
   ```

   Create `"$READER_AUTH_DIR/users.yml"` with mode `0600`. Enter the Argon2id
   output in the quoted password field. The allowlist uses the literal
   username `admin`.

   ```yaml
   users:
     admin:
       displayname: Mail reader
       email: reader@example.com
       password: '<Argon2id hash>'
   ```

4. Validate the configuration and start the stack.

   ```sh
   docker compose config --quiet
   docker compose run --rm --no-deps authelia authelia config validate \
       --config /etc/authelia/reader.yml --config.experimental.filters=template
   docker compose up -d
   ```

5. Enroll the reader's authenticator from the private terminal. Import the
   printed TOTP URI into the authenticator through a trusted local path. Treat
   it as a credential and keep it out of transcripts and logs. Verify device
   and server time before testing TOTP.

   ```sh
   docker compose exec authelia authelia storage user totp generate admin \
       --issuer 'Himalaya Mail' --config /etc/authelia/reader.yml \
       --config.experimental.filters=template
   ```

6. Configure the existing Seoul HTTPS edge to preserve the original `Host` and
   forward only the approved FQDN to `http://127.0.0.1:18080`. In NGINX, for
   example, put these directives inside the HTTPS virtual host's `location /`
   with the deployment's actual port.

   ```nginx
   proxy_pass http://127.0.0.1:18080;
   proxy_set_header Host $host;
   ```

   Validate and reload the edge using its normal procedure. Keep the upstream
   ports on loopback. Use the edge's existing certificate issuer and renewal
   setup. For DNS-01, use a site-scoped provider credential on Seoul and
   verify a staging renewal and subsequent edge reload. Confirm the certificate
   chain, hostname and expiry from outside Seoul.

## Verify and operate

Run `python3 deploy/seoul/smoke_auth.py` from the repo on a Linux Docker host.
It uses a synthetic loopback reader and a dedicated disposable Compose
project. It checks failed unauthenticated and password-only access, forwarded
identity headers, protected routes, cookie flags, the remembered expiry,
restart persistence, logout and administrator revocation. The synthetic check
connects directly to the loopback proxy. Use a real browser for public TLS
and cookie behavior.

Before publishing the site, verify the login flow in a fresh browser profile.
Check that the remember option starts unchecked, attempt a direct attachment
request before and after password entry, then complete TOTP. With the remember
option selected, reopen the browser and verify access. Confirm that a second
profile still needs both factors and that logout at `/authelia/logout` revokes
the first profile. The 30-day expiry is measured from successful TOTP completion
for the remembered reader session. The ordinary Authelia session uses a
one-hour cookie and a 15-minute inactivity limit.

Use the following command to revoke all sessions for this single-user
deployment. It clears only this project's dedicated Redis database.

```sh
docker compose exec -T redis redis-cli FLUSHDB SYNC
```

After changing the password hash or rotating a TOTP enrollment, run this
command. To recover a lost authenticator, verify the operator's identity,
revoke the sessions, and run the enrollment command with `--force` to replace
the old TOTP seed. The SQLite database and its storage key must be backed up
together. A lost storage key requires enrollment again.

## Roll back

Remove this dedicated hostname's route from the shared edge, or point it back
to the previous verified authenticated release. Validate and reload the edge,
then stop the new project with `docker compose down` and stop its reader
service if retiring it. Retain the named volumes and the private directory
for recovery. Confirm the hostname no longer serves the retired release and
check another virtual host on the edge.
