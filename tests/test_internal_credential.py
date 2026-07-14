"""The console chat's first-party credential must always be valid.

The admin chat page drives the embed API with a fixed ``(admin-chat, admin)``
pair. ``is_valid_credential`` is fail-closed, so the moment any other credential
exists that pair is rejected unless it is seeded too. ``ensure_internal_credential``
seeds it on every boot; it must also stay hidden from the Tenants list.
"""

import asyncio

from app import cache


def run(coro):
    cache._client = None
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        cache._client = None


def test_internal_credential_is_valid_even_with_other_creds():
    async def go():
        c = cache.get_client()
        await c.delete(cache._CRED_KEY)
        # an operator has registered a real credential -> store is non-empty ->
        # fail-closed rejects anything unseeded
        await cache.register_credential("acme", "secret")
        assert not await cache.is_valid_credential(
            cache.INTERNAL_CHAT_EMBED_ID, cache.INTERNAL_CHAT_PUBLIC_KEY
        )
        await cache.ensure_internal_credential()
        ok = await cache.is_valid_credential(
            cache.INTERNAL_CHAT_EMBED_ID, cache.INTERNAL_CHAT_PUBLIC_KEY
        )
        listed = await cache.list_credentials()
        await cache.close_client()
        return ok, listed

    ok, listed = run(go())
    assert ok, "admin chat credential must validate after seeding"
    # not surfaced as a customer tenant
    assert cache.INTERNAL_CHAT_EMBED_ID not in listed
    assert "acme" in listed
