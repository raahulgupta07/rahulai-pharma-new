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

**There is no self-signup, and by default an external login never creates a
user.** If someone authenticates successfully against Keycloak but has no `users`
row, the login is *refused*:

> no account for this email — ask an administrator to create one

Create the user in the admin panel first (Users → New), with the email exactly as
the IdP will report it. The consequence: a person who administers the Keycloak
realm cannot create a pharmacy admin by adding a realm user. They would also need
admin access here.

Order of checks on `POST /auth/login`: local password first, then LDAP if enabled.
The Keycloak button is a separate route (`/auth/sso/login`).

### Just-in-time provisioning (optional, OFF by default)

Hand-creating every user is fine for a handful of pharmacists and painful for a
site. Two runtime settings — **`oidc_auto_create`** and **`ldap_auto_create`**,
per source, both `false` unless you turn them on in Configuration →
Authentication — change the refusal above into a *provisioning* step:

* an unknown email that the IdP has authenticated gets a `users` row;
* the row is always **`role = 'user'`** — no Keycloak claim, group or mapper can
  make it an admin, because the role is a literal in the INSERT and is not a
  parameter of any function on that path;
* the row is always **`approved = false`**, so the person lands on the *pending
  administrator approval* screen and reaches nothing until an admin approves
  them (Users → Access → Approve). **JIT removes the typing, not the approval.**
* an `auth_events` row (`user_autocreate`) records the source and the email.

What it does *not* do: it never touches an existing row. A disabled account is
still refused rather than resurrected, an existing role is never rewritten, and
the email is normalised (trimmed + lower-cased) before the lookup, so JIT cannot
create a second row shadowing a user whose address differs only in case.

### Sign-in mode

**`signin_mode`** (runtime setting; also reported by public `GET /auth/config` so
the login screen renders the right controls):

| mode | effect |
|---|---|
| `local` | SSO is not offered; `GET /auth/sso/login` answers **403**, and a callback already in flight is refused too. |
| `hybrid` | **default** — password and SSO both available. Today's behaviour. |
| `sso_only` | password sign-in is refused *after* the password is verified, with a "sign in with single sign-on" message (**403**). |

⚠️ **`sso_only` always exempts a `super_admin`.** Without that carve-out, one
mis-typed discovery URL locks every human out of the console — including the
person who has to fix the realm — and the only way back in is editing Redis on
the box. The break-glass account keeps its local password. The check runs *after*
authentication, never before, so an anonymous caller cannot use the difference in
responses to learn which addresses are super_admins.

### Provider branding

**`oidc_provider_type`** — one of `keycloak` (default), `entra`, `google`,
`generic`. Cosmetic only: it picks the logo and the default button label on the
login screen. `oidc_provider_name` remains the display label ("Sign in with
…"). Both are returned by `GET /auth/config` and `GET /admin/auth-overview`.

All five settings live in the same effective-config layer as the rest of the
`ldap_*` / `oidc_*` keys (env default, overlaid by a Redis override written from
the admin page), are read fresh on every login, and need no restart.

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
> client with no secret. Turn it On anyway — a confidential client is one more
> thing an attacker has to have.
>
> The backend no longer *depends* on that, though. It used to skip `id_token`
> signature verification, on the reasoning that the code is redeemed over TLS
> authenticated with `client_secret` — a guarantee a public client silently
> removes. The `id_token` is now **verified against the realm JWKS**
> (`jwks_uri` from this discovery document): RS256/ES256 only, `iss` must match
> the realm, and the client must be named in `aud` **or** in `azp` — Keycloak
> puts `aud: "account"` in the id_token and identifies the client in `azp`, so a
> plain audience check would reject every real login. The authorize request also
> carries a `nonce`, which must come back in the id_token.
>
> Key rotation is handled by refetching `jwks_uri` **once** when a token arrives
> with an unknown `kid`. There is no "use the first key in the set" fallback: on
> a rotation the first key is the new one and the token in hand is signed by the
> old, so such a fallback turns a clear "unknown key id" into a confusing
> signature error. A JWKS that cannot be fetched produces a 401, never a 500.

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
