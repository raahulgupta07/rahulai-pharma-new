"""Per-outlet embed snippet generator (static, pre-signed, download-and-go).

Pinned here:
  * the baked ``data-user`` object + ``data-user-sig`` verify against the same
    signer the backend uses at ``/session/create`` — a snippet a customer pastes
    must authenticate;
  * the snippet carries the store_id, so the widget is locked to that branch;
  * HTML-escaping in the single-quoted ``data-user`` attribute round-trips back
    to the exact object (entity-decode -> json.loads -> same dict).
"""

import html as _html
import json
import re

from app.admin import OutletSnippetRequest, _outlet_user, _snippet_html
from app.security import sign_user, verify_user_signature


def _req(store="20024-CC73"):
    return OutletSnippetRequest(
        store_id=store, embed_id="web", public_key="web",
        base_url="https://pharma.example.com",
    )


def test_snippet_signature_verifies():
    req = _req()
    sig = sign_user(_outlet_user(req.store_id))
    snippet = _snippet_html(req, sig)
    # the sig baked into the tag must verify against the baked user object
    assert f'data-user-sig="{sig}"' in snippet
    assert verify_user_signature(_outlet_user(req.store_id), sig)


def test_baked_user_roundtrips_and_carries_store():
    req = _req("20052-CCTLKK")
    sig = sign_user(_outlet_user(req.store_id))
    snippet = _snippet_html(req, sig)
    raw = re.search(r"data-user='([^']+)'", snippet).group(1)
    user = json.loads(_html.unescape(raw))         # what the browser hands the widget
    assert user["store_id"] == "20052-CCTLKK"
    # the object the widget re-sends still verifies (signature is over the object)
    assert verify_user_signature(user, sig)


def test_each_store_gets_a_distinct_signature():
    a = sign_user(_outlet_user("20024-CC73"))
    b = sign_user(_outlet_user("20052-CCTLKK"))
    assert a != b
