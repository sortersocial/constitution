#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["xrpl-py"]
# ///
"""
mint.py — XRPL SLG token issuance

Usage:
  python mint.py testnet   — full dry run on XRPL testnet (uses faucet)
  python mint.py setup     — generate mainnet cold/hot wallets, print addresses for funding
  python mint.py mint      — wait for genesis sunrise, then issue 1 SLG on mainnet
"""

import json, sys, time, pathlib
from datetime import datetime, timezone

from xrpl.clients import JsonRpcClient
from xrpl.wallet import Wallet, generate_faucet_wallet
from xrpl.models.transactions import AccountSet, AccountSetAsfFlag, TrustSet, Payment
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.requests import AccountInfo, AccountLines, GatewayBalances
from xrpl.transaction import submit_and_wait

GENESIS_MS = 1775364389892

CURRENCY = "SLG"
TOTAL_SUPPLY = "1"

TESTNET_URL = "https://s.altnet.rippletest.net:51234"
MAINNET_URL = "https://xrplcluster.com"

WALLETS_DIR = pathlib.Path(__file__).parent


def wallets_path(network: str) -> pathlib.Path:
    if network == "testnet":
        return WALLETS_DIR / "testnet_wallets.json"
    return WALLETS_DIR / "wallets.json"


def save_wallets(cold: Wallet, hot: Wallet, network: str):
    data = {
        "network": network,
        "genesis_ms": GENESIS_MS,
        "currency": CURRENCY,
        "cold": {
            "classic_address": cold.classic_address,
            "seed": cold.seed,
        },
        "hot": {
            "classic_address": hot.classic_address,
            "seed": hot.seed,
        },
    }
    path = wallets_path(network)
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"  Wallets saved to {path}")


def load_wallets(network: str) -> tuple[Wallet, Wallet]:
    path = wallets_path(network)
    data = json.loads(path.read_text())
    cold = Wallet.from_seed(data["cold"]["seed"])
    hot = Wallet.from_seed(data["hot"]["seed"])
    assert data["network"] == network, f"wallet file is for {data['network']}, expected {network}"
    return cold, hot


# ---------------------------------------------------------------------------
# Transaction helpers
# ---------------------------------------------------------------------------

def configure_cold_wallet(client: JsonRpcClient, cold: Wallet):
    """Enable DefaultRipple so SLG can flow between holders."""
    tx = AccountSet(
        account=cold.classic_address,
        set_flag=AccountSetAsfFlag.ASF_DEFAULT_RIPPLE,
    )
    print(f"  Configuring cold wallet (DefaultRipple)...")
    result = submit_and_wait(tx, client, cold)
    status = result.result["meta"]["TransactionResult"]
    print(f"    {status}")
    assert status == "tesSUCCESS", f"cold wallet config failed: {status}"
    return result


def configure_hot_wallet(client: JsonRpcClient, hot: Wallet):
    """Enable RequireAuth to prevent hot wallet from accidentally issuing."""
    tx = AccountSet(
        account=hot.classic_address,
        set_flag=AccountSetAsfFlag.ASF_REQUIRE_AUTH,
    )
    print(f"  Configuring hot wallet (RequireAuth)...")
    result = submit_and_wait(tx, client, hot)
    status = result.result["meta"]["TransactionResult"]
    print(f"    {status}")
    assert status == "tesSUCCESS", f"hot wallet config failed: {status}"
    return result


def create_trust_line(client: JsonRpcClient, hot: Wallet, cold: Wallet):
    """Hot wallet trusts cold wallet for TOTAL_SUPPLY SLG."""
    tx = TrustSet(
        account=hot.classic_address,
        limit_amount=IssuedCurrencyAmount(
            currency=CURRENCY,
            issuer=cold.classic_address,
            value=TOTAL_SUPPLY,
        ),
    )
    print(f"  Creating trust line: hot trusts cold for {TOTAL_SUPPLY} {CURRENCY}...")
    result = submit_and_wait(tx, client, hot)
    status = result.result["meta"]["TransactionResult"]
    print(f"    {status}")
    assert status == "tesSUCCESS", f"trust line failed: {status}"
    return result


def issue_token(client: JsonRpcClient, cold: Wallet, hot: Wallet):
    """Cold wallet sends TOTAL_SUPPLY SLG to hot wallet — this IS the mint."""
    tx = Payment(
        account=cold.classic_address,
        destination=hot.classic_address,
        amount=IssuedCurrencyAmount(
            currency=CURRENCY,
            issuer=cold.classic_address,
            value=TOTAL_SUPPLY,
        ),
    )
    print(f"  Issuing {TOTAL_SUPPLY} {CURRENCY}: cold -> hot...")
    result = submit_and_wait(tx, client, cold)
    status = result.result["meta"]["TransactionResult"]
    print(f"    {status}")
    assert status == "tesSUCCESS", f"token issuance failed: {status}"
    return result


def verify(client: JsonRpcClient, cold: Wallet, hot: Wallet):
    """Check balances and trust lines after issuance."""
    print("\n  Verification:")

    lines_resp = client.request(AccountLines(
        account=hot.classic_address,
        ledger_index="validated",
    ))
    for line in lines_resp.result.get("lines", []):
        if line["currency"] == CURRENCY:
            print(f"    Hot wallet holds: {line['balance']} {CURRENCY} (issuer: {line['account']})")

    gw_resp = client.request(GatewayBalances(
        account=cold.classic_address,
        ledger_index="validated",
        hotwallet=[hot.classic_address],
    ))
    obligations = gw_resp.result.get("obligations", {})
    if CURRENCY in obligations:
        print(f"    Cold wallet obligations: {obligations[CURRENCY]} {CURRENCY}")

    for label, addr in [("Cold", cold.classic_address), ("Hot", hot.classic_address)]:
        info = client.request(AccountInfo(account=addr))
        balance_xrp = int(info.result["account_data"]["Balance"]) / 1_000_000
        print(f"    {label} XRP balance: {balance_xrp} XRP")


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def run_testnet():
    print("=" * 60)
    print("TESTNET -- full dry run")
    print("=" * 60)

    client = JsonRpcClient(TESTNET_URL)

    print("\n1. Funding wallets from faucet...")
    cold = generate_faucet_wallet(client, debug=True)
    hot = generate_faucet_wallet(client, debug=True)
    print(f"   Cold (issuer):      {cold.classic_address}")
    print(f"   Hot (operational):  {hot.classic_address}")

    print("\n2. Configuring cold wallet...")
    configure_cold_wallet(client, cold)

    print("\n3. Configuring hot wallet...")
    configure_hot_wallet(client, hot)

    print("\n4. Creating trust line...")
    create_trust_line(client, hot, cold)

    print("\n5. Issuing token...")
    result = issue_token(client, cold, hot)
    tx_hash = result.result.get("hash", "unknown")
    print(f"    TX hash: {tx_hash}")

    print("\n6. Verifying...")
    verify(client, cold, hot)

    save_wallets(cold, hot, "testnet")
    print("\nTestnet run complete.")


def run_setup():
    print("=" * 60)
    print("MAINNET -- wallet setup")
    print("=" * 60)

    cold = Wallet.create()
    hot = Wallet.create()

    save_wallets(cold, hot, "mainnet")

    genesis_dt = datetime.fromtimestamp(GENESIS_MS / 1000, tz=timezone.utc)

    print(f"\n  Cold wallet (issuer):      {cold.classic_address}")
    print(f"  Hot wallet (operational):  {hot.classic_address}")
    print()
    print("  Fund these addresses with XRP before running 'mint':")
    print(f"    Cold: send >= 1.5 XRP to {cold.classic_address}")
    print(f"    Hot:  send >= 1.5 XRP to {hot.classic_address}")
    print()
    print("  Reserve breakdown:")
    print("    1 XRP base reserve per account")
    print("    0.2 XRP owner reserve per trust line (first 2 free)")
    print("    ~0.00006 XRP for transaction fees")
    print("    Total minimum: ~2.1 XRP across both wallets")
    print()
    print(f"  Genesis: {genesis_dt.isoformat()}")
    print(f"  Genesis unix_ms: {GENESIS_MS}")
    print()
    print(f"  WARNING: seeds saved to {wallets_path('mainnet')} -- KEEP THIS SAFE")
    print(f"           add wallets.json to .gitignore")


def run_mint():
    print("=" * 60)
    print("MAINNET -- mint at genesis sunrise")
    print("=" * 60)

    cold, hot = load_wallets("mainnet")
    client = JsonRpcClient(MAINNET_URL)

    print(f"\n  Cold: {cold.classic_address}")
    print(f"  Hot:  {hot.classic_address}")

    for label, addr in [("Cold", cold.classic_address), ("Hot", hot.classic_address)]:
        try:
            info = client.request(AccountInfo(account=addr))
            balance_xrp = int(info.result["account_data"]["Balance"]) / 1_000_000
            print(f"  {label} balance: {balance_xrp} XRP")
            if balance_xrp < 1.0:
                print(f"  ERROR: {label} wallet below 1 XRP reserve. Fund it first.")
                sys.exit(1)
        except Exception as e:
            print(f"  ERROR: {label} wallet not found on ledger. Send XRP to activate it.")
            print(f"         {e}")
            sys.exit(1)

    genesis_dt = datetime.fromtimestamp(GENESIS_MS / 1000, tz=timezone.utc)
    print(f"\n  Genesis target: {genesis_dt.isoformat()}")
    print(f"  Genesis unix_ms: {GENESIS_MS}")

    now_ms = int(time.time() * 1000)
    wait_ms = GENESIS_MS - now_ms

    if wait_ms > 0:
        print(f"  Waiting {wait_ms / 1000:.1f}s until genesis...\n")
        while True:
            now_ms = int(time.time() * 1000)
            remaining = GENESIS_MS - now_ms
            if remaining <= 0:
                break
            if remaining > 60_000:
                print(f"    T-{remaining / 1000:.0f}s")
                time.sleep(min(remaining / 1000 - 30, 60))
            elif remaining > 5_000:
                print(f"    T-{remaining / 1000:.1f}s")
                time.sleep(1)
            else:
                time.sleep(remaining / 1000)

    launch_ts = datetime.now(timezone.utc)
    print(f"\n  GENESIS -- {launch_ts.isoformat()}")

    print("\n  Step 1/4: Configure cold wallet...")
    configure_cold_wallet(client, cold)

    print("\n  Step 2/4: Configure hot wallet...")
    configure_hot_wallet(client, hot)

    print("\n  Step 3/4: Create trust line...")
    create_trust_line(client, hot, cold)

    print("\n  Step 4/4: Issue 1 SLG...")
    result = issue_token(client, cold, hot)

    tx_hash = result.result.get("hash", "unknown")
    meta = result.result.get("meta", {})
    ledger_idx = result.result.get("validated_ledger_index", "unknown")

    print(f"\n  SLG minted.")
    print(f"    TX hash:      {tx_hash}")
    print(f"    Ledger index: {ledger_idx}")

    print("\n  Step 5: Verify...")
    verify(client, cold, hot)

    print(f"\n  GENESIS_MS = {GENESIS_MS}")
    print(f"  Submitted at:  {launch_ts.isoformat()}")
    print(f"  1 {CURRENCY} is live on XRPL mainnet.")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("testnet", "setup", "mint"):
        print("Usage: python mint.py [testnet|setup|mint]")
        print()
        print("  testnet  — full dry run on XRPL testnet (free, uses faucet)")
        print("  setup    — generate mainnet wallets, print addresses for funding")
        print("  mint     — wait for genesis sunrise, then issue 1 SLG on mainnet")
        sys.exit(1)

    {"testnet": run_testnet, "setup": run_setup, "mint": run_mint}[sys.argv[1]]()
