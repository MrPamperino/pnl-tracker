#!/usr/bin/env python3
"""Refresh big_positions.json (balances for the 'big positions' page, big-positions.html).

  python3 update_big_positions.py            # refresh balances, write JSON, commit + push if changed
  python3 update_big_positions.py --dry-run  # print summary only, no writes
  python3 update_big_positions.py --no-push  # write JSON, no git

Balances: Solana via public RPCs (rotating, retries); EVM native via public RPCs on many chains,
ERC-20 on Ethereum via eth.blockscout.com discovery + balanceOf on TRACKED tokens.
Prices (snapshot only; the page re-prices live): DefiLlama, fallback Jupiter (Solana).
Positions under THRESHOLD_USD and tokens without a real price (spam/airdrops) are left out.
Cost basis is unknown -> not stored (cost: null). Never prints secrets.
"""
import json, os, sys, time, subprocess, datetime, zoneinfo

REPO = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(REPO, "big_positions.json")
TZ = zoneinfo.ZoneInfo("Europe/Lisbon")
THRESHOLD_USD = 100.0
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"

SOLANA_WALLETS = ["Ay1vrqfSSmn5JYz7viZcKmki751bEh7v5V4WPp67nMFi"]
EVM_WALLETS = ["0xFaD2A6e902154CA7675d0Df4e0F6F091cf3aB69D", "0xA74C0C14E29a2aE6fc45c0de80D5D7BC469B133C",
               "0xaeF0939EFAE51BfD325bC38E9Dc9FB0df09B1d46", "0xc14346768592DddD9cD6d6964916Ed93a553810F",
               "0x7Efa55f129Eb43477aA0309C106120F55dE8684F",
               "0x9F0d7B28D30B4a3936e0288948dd29d32c85D2B0",   # also the AERO wallet of the main tracker (portfolio.json untouched)
               "0x4c34c3fd980Ba9B4304BcD4C284f55f852638a1b"]
# Per-wallet allowlist: only these (chain, token) pairs are kept for the wallet (token None = native coin).
# 0x9F0d…D2B0: big-positions shows only its ETH (stETH + native ETH); its AERO lives in the main tracker (portfolio.json).
WALLET_ONLY = {
    "0x9F0d7B28D30B4a3936e0288948dd29d32c85D2B0": {
        ("Ethereum", "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84".lower()),   # stETH
        ("Ethereum", None),                                                   # native ETH
    },
}
MANUAL = [{"symbol": "BTC", "name": "Bitcoin", "qty": 12.02, "priceKey": "coingecko:bitcoin",
           "note": "manual · off-chain (no address given)"}]

# chain -> (rpc urls, native symbol, DefiLlama price key for native, explorer address url)
EVM_CHAINS = {
    "Ethereum": (["https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com"], "ETH", "coingecko:ethereum", "https://etherscan.io/address/"),
    "Base": (["https://mainnet.base.org", "https://base-rpc.publicnode.com"], "ETH", "coingecko:ethereum", "https://basescan.org/address/"),
    "Arbitrum": (["https://arb1.arbitrum.io/rpc", "https://arbitrum-one-rpc.publicnode.com"], "ETH", "coingecko:ethereum", "https://arbiscan.io/address/"),
    "Optimism": (["https://mainnet.optimism.io"], "ETH", "coingecko:ethereum", "https://optimistic.etherscan.io/address/"),
    "Polygon": (["https://polygon-bor-rpc.publicnode.com", "https://polygon-rpc.com"], "POL", "coingecko:polygon-ecosystem-token", "https://polygonscan.com/address/"),
    "BSC": (["https://bsc-rpc.publicnode.com", "https://bsc-dataseed.bnbchain.org"], "BNB", "coingecko:binancecoin", "https://bscscan.com/address/"),
    "Avalanche": (["https://api.avax.network/ext/bc/C/rpc"], "AVAX", "coingecko:avalanche-2", "https://snowtrace.io/address/"),
    "Linea": (["https://rpc.linea.build"], "ETH", "coingecko:ethereum", "https://lineascan.build/address/"),
    "Scroll": (["https://rpc.scroll.io"], "ETH", "coingecko:ethereum", "https://scrollscan.com/address/"),
    "Blast": (["https://rpc.blast.io"], "ETH", "coingecko:ethereum", "https://blastscan.io/address/"),
    "zkSync": (["https://mainnet.era.zksync.io"], "ETH", "coingecko:ethereum", "https://era.zksync.network/address/"),
    "Unichain": (["https://mainnet.unichain.org"], "ETH", "coingecko:ethereum", "https://uniscan.xyz/address/"),
    "Mantle": (["https://rpc.mantle.xyz"], "MNT", "coingecko:mantle", "https://mantlescan.xyz/address/"),
    "Sonic": (["https://rpc.soniclabs.com"], "S", "coingecko:sonic-3", "https://sonicscan.org/address/"),
    "Gnosis": (["https://rpc.gnosischain.com"], "xDAI", "coingecko:xdai", "https://gnosisscan.io/address/"),
    "HyperEVM": (["https://rpc.hyperliquid.xyz/evm"], "HYPE", "coingecko:hyperliquid", "https://hyperevmscan.io/address/"),
}
# ERC-20s always checked with balanceOf (chain, address, symbol, name, decimals, DefiLlama key)
TRACKED = [
    ("Ethereum", "0x28B3a8fb53B741A8Fd78c0fb9A6B2393d896a43d", "spUSDC", "Spark Savings USDC", 6, "ethereum:0x28B3a8fb53B741A8Fd78c0fb9A6B2393d896a43d"),
    ("Ethereum", "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84", "stETH", "Lido Staked ETH", 18, "ethereum:0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"),
    ("Ethereum", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "USDC", "USD Coin", 6, "ethereum:0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
    ("Ethereum", "0xdAC17F958D2ee523a2206206994597C13D831ec7", "USDT", "Tether", 6, "ethereum:0xdAC17F958D2ee523a2206206994597C13D831ec7"),
    ("Ethereum", "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0", "wstETH", "Wrapped stETH", 18, "ethereum:0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"),
    ("Ethereum", "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", "WBTC", "Wrapped BTC", 8, "ethereum:0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"),
    ("Ethereum", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "WETH", "Wrapped Ether", 18, "ethereum:0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"),
    ("Ethereum", "0xa3931d71877C0E7a3148CB7Eb4463524FEc27fbD", "sUSDS", "Savings USDS", 18, "ethereum:0xa3931d71877C0E7a3148CB7Eb4463524FEc27fbD"),
    ("Ethereum", "0x83F20F44975D03b1b09e64809B757c47f942BEeA", "sDAI", "Savings Dai", 18, "ethereum:0x83F20F44975D03b1b09e64809B757c47f942BEeA"),
    ("Ethereum", "0x9D39A5DE30e57443BfF2A8307A4256c8797A3497", "sUSDe", "Ethena Staked USDe", 18, "ethereum:0x9D39A5DE30e57443BfF2A8307A4256c8797A3497"),
    # Base (no scripted token discovery there: explorer blocks bots -> check known tokens)
    ("Base", "0x940181a94A35A4569E4529A3CDfB74e38FD98631", "AERO", "Aerodrome Finance", 18, "base:0x940181a94A35A4569E4529A3CDfB74e38FD98631"),
    ("Base", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USDC", "USD Coin (Base)", 6, "base:0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
    ("Base", "0x4200000000000000000000000000000000000006", "WETH", "Wrapped Ether (Base)", 18, "base:0x4200000000000000000000000000000000000006"),
    ("Base", "0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452", "wstETH", "Bridged wstETH (Base)", 18, "base:0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452"),
]
SOL_RPCS = ["https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com", "https://solana.drpc.org"]
SOL = "So11111111111111111111111111111111111111112"

errors = []

def log(*a):
    print(*a, file=sys.stderr)

def curl_json(url, data=None, timeout=25):
    cmd = ["curl", "-s", "-L", "-m", str(timeout), "-A", UA, "-H", "accept: application/json"]
    if data is not None:
        cmd += ["-X", "POST", "-H", "content-type: application/json", "-d", json.dumps(data)]
    out = subprocess.run(cmd + [url], capture_output=True, text=True).stdout
    return json.loads(out)

def evm_batch(urls, calls):
    """calls: list of (method, params). Batches of 10. Returns results list (None on failure)."""
    res = [None] * len(calls)
    for i in range(0, len(calls), 10):
        chunk = calls[i:i + 10]
        payload = [{"jsonrpc": "2.0", "id": j, "method": m, "params": p} for j, (m, p) in enumerate(chunk)]
        ok = False
        for attempt in range(6):
            u = urls[attempt % len(urls)]
            try:
                r = curl_json(u, payload)
                if isinstance(r, list) and all("result" in x for x in r):
                    for x in r:
                        res[i + x["id"]] = x["result"]
                    ok = True
                    break
            except Exception:
                pass
            time.sleep(1 + attempt)
        if not ok:
            return None
    return res

def sol_rpc(method, params):
    for i in range(9):
        try:
            r = curl_json(SOL_RPCS[i % len(SOL_RPCS)], {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if "result" in r:
                return r["result"]
        except Exception:
            pass
        time.sleep(1 + i)
    raise RuntimeError(f"solana {method} failed on all RPCs")

def llama_prices(keys):
    out = {}
    keys = list(dict.fromkeys(keys))
    for i in range(0, len(keys), 60):
        for attempt in range(3):
            try:
                d = curl_json("https://coins.llama.fi/prices/current/" + ",".join(keys[i:i + 60]))
                for k, v in (d.get("coins") or {}).items():
                    if v.get("price") and v.get("confidence", 1) >= 0.8:
                        out[k] = {"price": float(v["price"]), "symbol": v.get("symbol")}
                break
            except Exception:
                time.sleep(2 + 2 * attempt)
    return out

def jup_info(mints):
    out = {}
    for i in range(0, len(mints), 50):
        for attempt in range(4):
            try:
                d = curl_json("https://lite-api.jup.ag/tokens/v2/search?query=" + ",".join(mints[i:i + 50]))
                for t in d:
                    out[t["id"]] = t
                break
            except Exception:
                time.sleep(3 + 3 * attempt)
    return out

# ---------------------------------------------------------------- discovery
def solana_wallet(w):
    raw = []
    for prog in ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]:
        for a in sol_rpc("getTokenAccountsByOwner", [w, {"programId": prog}, {"encoding": "jsonParsed"}])["value"]:
            i = a["account"]["data"]["parsed"]["info"]
            q = float(i["tokenAmount"]["uiAmountString"] or 0)
            if q > 0:
                raw.append((i["mint"], q))
    sol = sol_rpc("getBalance", [w])["value"] / 1e9
    hold = [{"symbol": "SOL", "name": "Solana", "chain": "Solana", "token": SOL, "qty": sol, "priceKey": "coingecko:solana"}]
    info = jup_info([m for m, _ in raw])
    for m, q in raw:
        t = info.get(m, {})
        hold.append({"symbol": t.get("symbol") or m[:4] + "…", "name": t.get("name") or "", "chain": "Solana",
                     "token": m, "qty": q, "priceKey": "solana:" + m,
                     "_jup": {"price": t.get("usdPrice"), "liq": t.get("liquidity"), "verified": t.get("isVerified")}})
    return hold

def evm_wallet(w):
    hold = []
    pad = w[2:].lower().rjust(64, "0")
    for ch, (urls, sym, key, _) in EVM_CHAINS.items():
        calls = [("eth_getBalance", [w, "latest"])]
        toks = [t for t in TRACKED if t[0] == ch]
        calls += [("eth_call", [{"to": t[1], "data": "0x70a08231" + pad}, "latest"]) for t in toks]
        r = evm_batch(urls, calls)
        if r is None:
            errors.append(f"{ch} RPC failed for {w}")
            continue
        nat = int(r[0], 16) / 1e18
        if nat > 0:
            hold.append({"symbol": sym, "name": f"{sym} (native)", "chain": ch, "token": None, "qty": nat, "priceKey": key})
        for t, v in zip(toks, r[1:]):
            q = int(v or "0x0", 16) / 10 ** t[4]
            if q > 0:
                hold.append({"symbol": t[2], "name": t[3], "chain": ch, "token": t[1], "qty": q, "priceKey": t[5]})
    # Ethereum ERC-20 discovery (new tokens not in TRACKED)
    try:
        d = curl_json(f"https://eth.blockscout.com/api/v2/addresses/{w}/tokens?type=ERC-20")
        have = {(h["chain"], (h["token"] or "").lower()) for h in hold}
        cand = []
        for it in d.get("items") or []:
            t = it["token"]
            if ("Ethereum", t["address_hash"].lower()) in have or not t.get("exchange_rate"):
                continue   # no market price -> spam/airdrop
            q = int(it["value"]) / 10 ** int(t.get("decimals") or 0)
            if q * float(t["exchange_rate"]) >= THRESHOLD_USD:
                cand.append(t)
        if cand:   # Blockscout balances can be stale -> confirm each one on-chain with balanceOf
            r = evm_batch(EVM_CHAINS["Ethereum"][0], [("eth_call", [{"to": t["address_hash"], "data": "0x70a08231" + pad}, "latest"]) for t in cand])
            if r is None:
                errors.append(f"Ethereum balanceOf check failed for discovered tokens of {w}")
                r = []
            for t, v in zip(cand, r):
                q = int(v or "0x0", 16) / 10 ** int(t.get("decimals") or 0)
                if q > 0:
                    hold.append({"symbol": t["symbol"], "name": t["name"], "chain": "Ethereum", "token": t["address_hash"],
                                 "qty": q, "priceKey": "ethereum:" + t["address_hash"], "_note": "discovered via Blockscout"})
    except Exception as e:
        errors.append(f"Ethereum token discovery failed for {w}: {type(e).__name__}")
    return hold

def short(a):
    return f"{a[:4]}…{a[-4:]}" if a.startswith("0x") is False else f"{a[:6]}…{a[-4:]}"

def _prev_file():
    try:
        return json.load(open(OUT))
    except Exception:
        return {}

def main():
    args = set(sys.argv[1:])
    wallets = []
    for w in SOLANA_WALLETS:
        try:
            wallets.append({"id": w[:8].lower(), "label": short(w), "chain": "Solana", "address": w,
                            "explorer": "https://solscan.io/account/" + w, "holdings": solana_wallet(w)})
        except Exception as e:
            # keep the last known balances instead of showing an empty wallet
            errors.append(f"Solana {w}: {e} (kept previous balances)")
            prev_w = next((x for x in _prev_file().get("wallets", []) if x.get("address") == w), None)
            prev_h = [{k: v for k, v in h.items() if k not in ("price", "value", "cost")} for h in (prev_w or {}).get("holdings", [])]
            wallets.append({"id": w[:8].lower(), "label": short(w), "chain": "Solana", "address": w,
                            "explorer": "https://solscan.io/account/" + w, "holdings": prev_h,
                            "error": "RPC falhou — saldos da atualização anterior" + (f" ({_prev_file().get('updated')})" if prev_w else "")})
    for w in EVM_WALLETS:
        hold = evm_wallet(w)
        allow = WALLET_ONLY.get(w)
        if allow is not None:   # drop everything not allow-listed (not shown, not in skipped)
            hold = [h for h in hold if (h["chain"], (h["token"] or "").lower() or None) in allow]
        wallets.append({"id": w[:8].lower(), "label": short(w), "chain": "EVM", "address": w,
                        "explorer": "https://debank.com/profile/" + w, "holdings": hold,
                        **({"note": "só ETH (stETH + ETH nativo)"} if allow is not None else {})})
    keys = [h["priceKey"] for wl in wallets for h in wl["holdings"]] + [m["priceKey"] for m in MANUAL]
    px = llama_prices(keys)
    # stable symbols/names: previous file > Jupiter > DefiLlama
    try:
        _old = json.load(open(OUT))
        prev = {h["priceKey"]: h for w in _old["wallets"] for h in w["holdings"]}
        prev.update({x["priceKey"]: x for x in _old.get("skipped", []) if x.get("priceKey") and not x["symbol"].endswith("…")})
    except Exception:
        prev = {}
    for wl in wallets:
        for h in wl["holdings"]:
            if h["symbol"].endswith("…"):
                h["symbol"] = (prev.get(h["priceKey"]) or {}).get("symbol") or (px.get(h["priceKey"]) or {}).get("symbol") or h["symbol"]
            if not h.get("name"):
                h["name"] = (prev.get(h["priceKey"]) or {}).get("name") or ""
    skipped = []
    for wl in wallets:
        kept = []
        for h in wl["holdings"]:
            p = (px.get(h["priceKey"]) or {}).get("price")
            j = h.pop("_jup", None)
            if p is None and j and j.get("price") and j.get("verified"):
                p = float(j["price"])
            h["price"] = p
            h["value"] = round(h["qty"] * p, 2) if p else None
            h["cost"] = None
            h.pop("_note", None)
            if p is None or h["value"] < THRESHOLD_USD:
                skipped.append({"wallet": wl["label"], "chain": h["chain"], "symbol": h["symbol"], "priceKey": h["priceKey"], "qty": h["qty"],
                                "value": h["value"], "reason": "no price (spam/airdrop)" if p is None else f"< ${THRESHOLD_USD:.0f}"})
                continue
            kept.append(h)
        kept.sort(key=lambda h: -h["value"])
        wl["holdings"] = kept
        wl["value"] = round(sum(h["value"] for h in kept), 2)
    manual = []
    for m in MANUAL:
        p = (px.get(m["priceKey"]) or {}).get("price")
        manual.append(dict(m, price=p, value=round(m["qty"] * p, 2) if p else None, cost=None))
    total = round(sum(w["value"] for w in wallets) + sum(m["value"] or 0 for m in manual), 2)
    data = {"updated": datetime.datetime.now(TZ).isoformat(timespec="seconds"), "threshold_usd": THRESHOLD_USD,
            "snapshot_total_usd": total, "wallets": wallets, "manual": manual,
            "skipped": skipped, "errors": errors,
            "notes": "Balances refreshed by update_big_positions.py; the page re-prices live (DefiLlama, fallbacks). Cost basis unknown (cost: null)."}
    summary = {"total": total, "wallets": [(w["label"], w["value"], [(h["chain"], h["symbol"], round(h["qty"], 6), h["value"]) for h in w["holdings"]]) for w in wallets],
               "manual": [(m["symbol"], m["qty"], m["value"]) for m in manual], "errors": errors}
    if "--dry-run" in args:
        print(json.dumps(summary, indent=1, ensure_ascii=False)); return
    old = None
    try:
        old = json.load(open(OUT))
    except Exception:
        pass
    strip = lambda d: json.dumps([[(h["chain"], h["symbol"], round(h["qty"], 6)) for h in w["holdings"]] for w in d["wallets"]]) if d else None
    changed = strip(old) != strip(data)
    with open(OUT, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False); f.write("\n")
    if changed and "--no-push" not in args:
        g = lambda *a: subprocess.run(["git", "-C", REPO, *a], capture_output=True, text=True)
        g("add", "big_positions.json")
        g("-c", "user.name=New Bot", "-c", "user.email=bot@local", "commit", "-q", "-m", "Big positions: balances " + data["updated"])
        g("pull", "-q", "--rebase", "--autostash", "origin", "main")
        summary["push"] = g("push", "-q", "origin", "main").returncode == 0
    summary["changed"] = changed
    print(json.dumps(summary, ensure_ascii=False))

if __name__ == "__main__":
    main()
