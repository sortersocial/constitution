"""Signed snippets bind $slots as eval locals, never as spliced source.

Regression coverage for a remote-code-execution class: a request value that
contains a `$slot` reference must not be able to reintroduce a live slot and
realign string quotes so that attacker bytes land in code position. Values are
bound to eval locals, so they can only ever be data.
"""

import constitution as c


def _signed(template: str) -> dict:
    nonce = c.signer.generate_nonce()
    sig = c.signer.sign(template, nonce)
    return {"__snippet__": template, "__sig__": sig, "__nonce__": nonce}


def test_bind_snippet_rewrites_slots_and_binds_values():
    form = {**_signed("redeem('alice', $wallet_address)"), "wallet_address": "0xABC"}
    code, bindings = c._bind_snippet(form)
    assert code == "redeem('alice', wallet_address)"
    assert bindings == {"wallet_address": "0xABC"}


def test_bind_snippet_neutralizes_nested_slot_injection():
    # The historical RCE: wallet_address="$q" would, under string splicing,
    # reintroduce $q inside a scrubbed literal; scrubbing q's payload then
    # realigned quotes so __import__(...) became a live call argument.
    payload = "', __import__('os').system('touch /tmp/pwned'), '"
    form = {
        **_signed("redeem('alice', $wallet_address)"),
        "wallet_address": "$q",
        "q": payload,
    }
    code, bindings = c._bind_snippet(form)

    # Only the declared slot is bound; the unsigned payload field is ignored.
    assert bindings == {"wallet_address": "$q"}
    assert "q" not in bindings

    calls = []
    eval(code, {"redeem": lambda u, w: calls.append((u, w))}, bindings)
    # redeem is called exactly once, with the payload as an inert string.
    assert calls == [("alice", "$q")]


def test_bind_snippet_ignores_extra_fields_and_cannot_shadow_globals():
    form = {
        **_signed("redeem('alice', $wallet_address)"),
        "wallet_address": "0xABC",
        "redeem": "attacker",  # would shadow the global if it leaked into locals
        "os": "attacker",
    }
    _, bindings = c._bind_snippet(form)
    assert bindings == {"wallet_address": "0xABC"}


def test_bind_snippet_rejects_bad_signature():
    form = {
        "__snippet__": "redeem('alice', $wallet_address)",
        "__sig__": "not-a-real-signature",
        "__nonce__": c.signer.generate_nonce(),
        "wallet_address": "0xABC",
    }
    try:
        c._bind_snippet(form)
    except c.SnippetExecutionError as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("expected SnippetExecutionError for bad signature")


def test_bind_snippet_consumes_nonce_once():
    template = "redeem('alice', $wallet_address)"
    form = {**_signed(template), "wallet_address": "0xABC"}
    c._bind_snippet(form)
    try:
        c._bind_snippet(form)
    except c.SnippetExecutionError as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("nonce must be single-use")


def test_bind_snippet_requires_declared_slot_values():
    form = _signed("redeem('alice', $wallet_address)")  # no wallet_address field
    try:
        c._bind_snippet(form)
    except c.SnippetExecutionError as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("missing slot value must be rejected")
