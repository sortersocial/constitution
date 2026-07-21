"""Signed snippets bind $slots as eval locals, never as spliced source.

Regression coverage for a remote-code-execution class: a request value that
contains a `$slot` reference must not be able to reintroduce a live slot and
realign string quotes so that attacker bytes land in code position. Values are
bound to eval locals (via evaleval's BoundSnippet), so they can only ever be
data. These tests drive the exact path used by the POST / handler.
"""

import constitution as c


def _signed(template: str) -> dict:
    nonce = c.signer.generate_nonce()
    sig = c.signer.sign(template, nonce)
    return {"__snippet__": template, "__sig__": sig, "__nonce__": nonce}


def test_verify_snippet_binds_value_as_local_not_source():
    form = {**_signed("redeem('alice', $wallet_address)"), "wallet_address": "0xABC"}
    bound = c.signer.verify_snippet(form)
    assert "0xABC" not in bound.source  # value is not spliced into the source
    assert "0xABC" in bound.locals_.values()

    calls = []
    bound.eval({"redeem": lambda u, w: calls.append((u, w))})
    assert calls == [("alice", "0xABC")]


def test_nested_slot_payload_cannot_execute():
    # Historical RCE: wallet_address="$q" would, under string splicing,
    # reintroduce $q inside a scrubbed literal; scrubbing q's payload then
    # realigned quotes so __import__(...) became a live call argument.
    payload = "', __import__('os').system('touch /tmp/pwned'), '"
    form = {
        **_signed("redeem('alice', $wallet_address)"),
        "wallet_address": "$q",
        "q": payload,
    }
    bound = c.signer.verify_snippet(form)

    calls = []
    bound.eval({"redeem": lambda *a: calls.append(a) or "ok"})
    # redeem is called exactly once, with the payload as an inert string.
    assert calls == [("alice", "$q")]


def test_extra_fields_cannot_shadow_globals():
    # A submitted field named like a global must not leak into the eval scope
    # and shadow the real handler.
    form = {
        **_signed("redeem('alice', $wallet_address)"),
        "wallet_address": "0xABC",
        "redeem": "attacker",
        "os": "attacker",
    }
    bound = c.signer.verify_snippet(form)
    sentinel = object()
    called = []

    def redeem(u, w):
        called.append((u, w))
        return sentinel

    assert bound.eval({"redeem": redeem}) is sentinel
    assert called == [("alice", "0xABC")]


def test_bad_signature_is_rejected():
    form = {
        "__snippet__": "redeem('alice', $wallet_address)",
        "__sig__": "not-a-real-signature",
        "__nonce__": c.signer.generate_nonce(),
        "wallet_address": "0xABC",
    }
    try:
        c.signer.verify_snippet(form)
    except c.SnippetExecutionError as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("expected SnippetExecutionError for bad signature")


def test_nonce_is_single_use():
    form = {**_signed("redeem('alice', $wallet_address)"), "wallet_address": "0xABC"}
    c.signer.verify_snippet(form)
    try:
        c.signer.verify_snippet(form)
    except c.SnippetExecutionError as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("nonce must be single-use")
