# Keycloak SSO + LDAP — configuration guide

Audience: whoever deploys City Pharma Agent. Everything here is `.env` plus a few
clicks in Keycloak. No code changes.

---

## The identity model, in one paragraph

**One user row per email.** Email is the merge key. A person may have several
linked auth sources — `local` password, `ldap`, `oidc` — and logging in through
any of them resolves to the same row, so they keep one identity and one role.

**Keycloak and LDAP prove *who* someone is. The `users` table decides *what* they
may do.** Roles (`super_admin`, `admin`, `user`) live only in Postgres and are set
in the admin panel. Nothing in a Keycloak token or an LDAP group can grant them.

**There is no self-signup.** If someone authenticates successfully against
Keycloak but has no `users` row, the login is *refused*:

> no account for this email — ask an administrator to create one

This is deliberate and it is the main thing protecting you. Create the user in the
admin panel first (Users → New), with the email exactly as the IdP will report it.
The consequence: a person who administers the Keycloak realm cannot create a
pharmacy admin by adding a realm user. They would also need admin access here.

Order of checks on `POST /auth/login`: local password first, then LDAP if enabled.
The Keycloak button is a separate route (`/auth/sso/login`).

---

## Keycloak

### 1. Create the client

Realm → Clients → **Create client**

| Field | Value |
|---|---|
| Client type | `OpenID Connect` |
| Client ID | `pharmacy-agent` |
| Client authentication | **On** (this makes it a *confidential* client) |
| Standard flow | On |
| Direct access grants | Off |
| Valid redirect URIs | `https://pharmacy.example.com/auth/sso/callback` |
| Web origins | `https://pharmacy.example.com` |

> Keycloak 26 defaults **Client authentication** to Off, which yields a *public*
> client with no secret. Do not leave it there. The backend skips `id_token`
> signature verification precisely because it redeems the code over TLS
> authenticated with `client_secret`; a public client removes that guarantee.

Then Clients → `pharmacy-agent` → **Credentials** → copy the client secret.

### 2. Fill in `.env`

```dotenv
OIDC_ENABLED=true
OIDC_PROVIDER_NAME=Keycloak
OIDC_DISCOVERY_URL=https://keycloak.example.com/realms/citcare/.well-known/openid-configuration
OIDC_CLIENT_ID=pharmacy-agent
OIDC_CLIENT_SECRET=<from the Credentials tab>
OIDC_REDIRECT_URI=https://pharmacy.example.com/auth/sso/callback
OIDC_SCOPES=openid email profile
COOKIE_SECURE=true          # you are on https now
```

`OIDC_REDIRECT_URI` must byte-match a Valid Redirect URI in the client, or Keycloak
returns `invalid_redirect_uri` before it ever reaches us.

Path is `/realms/<realm>/...`. Keycloak ≤ 16 used `/auth/realms/<realm>/...`.

### 3. Provision the users

Admin panel → **Users** → New, one row per person, email matching the IdP's
`email` claim. Set the role there. Then restart the API and the login screen shows
**Sign in with Keycloak**.

### 4. Check it

```bash
curl -s https://pharmacy.example.com/auth/config
# {"ldap_enabled":false,"oidc_enabled":true,"oidc_provider_name":"Keycloak"}
```

If the user has no `users` row, the callback returns **401** with the
"ask an administrator" message. That is the system working.

---

## LDAP / Active Directory

```dotenv
LDAP_ENABLED=true
LDAP_HOST=ldap.corp.com
LDAP_PORT=636
LDAP_USE_SSL=true            # or LDAP_PORT=389 + LDAP_START_TLS=true
LDAP_VALIDATE_CERT=true
LDAP_CA_CERT_FILE=/etc/ssl/certs/corp-ca.pem   # only for a private CA
LDAP_BIND_DN=cn=svc-pharmacy,ou=service,dc=corp,dc=com
LDAP_BIND_PASSWORD=...
LDAP_BASE_DN=ou=users,dc=corp,dc=com
LDAP_USER_FILTER=(uid={username})
LDAP_EMAIL_ATTR=mail
LDAP_NAME_ATTR=cn
```

For Active Directory the filter is usually:

```dotenv
LDAP_USER_FILTER=(sAMAccountName={username})
```

`{username}` is substituted with the value from the login form and escaped
(`escape_filter_chars`), so a user cannot inject filter syntax.

**Never run this on plain 389 with no StartTLS.** The flow rebinds as the user to
verify their password, which means the password crosses the wire. With
`LDAP_VALIDATE_CERT=false` the certificate is not checked and anyone on the network
path can read it.

The service account only needs **read** on `LDAP_BASE_DN`. It searches; it does not
write.

### The flow

1. Bind as `LDAP_BIND_DN` (or anonymously if unset).
2. Search `LDAP_BASE_DN` with `LDAP_USER_FILTER` → the user's DN, `mail`, `cn`.
3. Rebind as *that DN* with the submitted password. This step, and only this step,
   proves the password.
4. Resolve `mail` to a `users` row by email. No row → refused.

LDAP is also the fallback for `POST /auth/login`: local password is tried first, and
if it fails and `LDAP_ENABLED=true`, the same email/password go to LDAP.

---

## Things that will bite you

**Empty passwords are rejected before the bind.** A simple bind with a valid DN and
a zero-length password is an *unauthenticated simple bind* (RFC 4513 §5.1.2). A
server configured to allow it — some Active Directory deployments — answers
**success**, so knowing any provisioned email would be enough to log in as that
person. (A default OpenLDAP refuses it server-side, and ldap3 refuses it
client-side, but neither is guaranteed on your directory.) The old code also let
that client-side refusal escape as an HTTP 500 on any wrong password. The guard in
`login_ldap` closes both; `tests/test_auth_sso.py` fails if you remove it.

**Keep a local password.** `ADMIN_EMAIL` / `ADMIN_PASSWORD` seed a `super_admin` on
first boot. If Keycloak is down, that is the only way in — including the way in to
fix the Keycloak settings. Change it from `changeme`. It is only used on first boot;
changing it later does not update an existing row.

**`SECRET_KEY` signs everything.** Admin JWTs, embed session tokens, the widget HMAC,
and the SSO `state`. It defaults to `dev-secret-change-me`, which is in the repo.
Set 32+ random bytes before exposing this to anyone. If you embed the widget, it must
equal Laravel's `CITYAGENT_SECRET_KEY`.

**Roles do not sync.** Deactivating someone in Keycloak stops them signing in, but
their `users` row stays. Deactivate there too (`active = false`), or their existing
JWT keeps working until it expires — `AUTH_TOKEN_TTL_HOURS`, default 12.

**Only one OIDC provider.** There is a single `OIDC_*` block. Keycloak can federate
Google/Entra upstream if you need more.

---

## Not covered by SSO

These are separate and still open on a public deploy:

- `is_valid_credential` (`app/cache.py`) returns **True for any credentials** while
  the Redis credential hash is empty — the embed widget is open until you register
  one.
- `ALLOWED_ORIGINS` defaults to `*`.
- `/metrics`, `/metrics/history`, `POST /api/embed/reload`, `POST /api/embed/ingest`
  are unauthenticated.
- When an embed session carries no signed `store_id`, tools run **unscoped**. Store
  scoping is only enforced for HMAC-signed users.
