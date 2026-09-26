#!/usr/bin/env python3
"""Keep wallet-5mef.html in sync with on-chain state of the 5Mef Solana wallet.

  python3 /workspace/pnl-tracker/update_5mef_page.py            # update + commit/push only if holdings/activity changed
  python3 /workspace/pnl-tracker/update_5mef_page.py --dry-run  # compute + print, no file writes, no git
  python3 /workspace/pnl-tracker/update_5mef_page.py --no-push  # write files, no git
  python3 /workspace/pnl-tracker/update_5mef_page.py --refresh-marks  # also publish when only prices moved

Truth: on-chain balances. Cost basis lives in _5mef_ledger.json:
  - new buys paid with USDC/USDT (or SOL / another token, valued at current price, flagged approx)
    add cost = amount spent;
  - sells reduce cost pro-rata (avg = cost / pre-trade on-chain qty) and add realized
    (proceeds only when cost unknown);
  - transfers in = zero cost; transfers out reduce cost pro-rata, no realized.
Every processed signature is stored in the ledger, so a trade is never counted twice.
Prints one JSON line summary at the end. Never prints secrets.
"""
import json, os, re, sys, time, subprocess, datetime, urllib.request, zoneinfo

REPO = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(REPO, "wallet-5mef.html")
LEDGER = os.path.join(REPO, "_5mef_ledger.json")
OWNER = "5MefLSkN3mhKVe3gtXHnz2gjAkrkn6P5r9ufL4wi849r"
TZ = zoneinfo.ZoneInfo("Europe/Lisbon")
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
STABLE = {USDC: "USDC", USDT: "USDT"}
SOL = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAMS = ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]
RPCS = ["https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com",
        "https://solana.drpc.org", "https://rpc.ankr.com/solana"]
MIN_LIQ = 10_000     # USD liquidity for an untracked token to be shown
MIN_VALUE = 1.0      # USD value for an untracked token to be shown
MAX_ACTIVITY = 30
UA = {"user-agent": "Mozilla/5.0", "content-type": "application/json"}

def log(*a):
    print(*a, file=sys.stderr)

def http_json(url, data=None, timeout=25):
    req = urllib.request.Request(url, json.dumps(data).encode() if data is not None else None, UA)
    return json.load(urllib.request.urlopen(req, timeout=timeout))

def rpc(method, params, tries=12):
    err = None
    for i in range(tries):
        url = RPCS[i % len(RPCS)]
        try:
            r = http_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if "result" in r:
                return r["result"]
            err = r.get("error")
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:120]}"
        time.sleep(min(1.5 + i, 8))
    raise RuntimeError(f"{method} failed on all RPCs: {err}")

def now_pt():
    return datetime.datetime.now(TZ)

def fmt(n, d=6):
    s = f"{n:,.{d}f}".rstrip("0").rstrip(".")
    return s or "0"

def short(a):
    return f"{a[:6]}…{a[-4:]}"

# ---------------------------------------------------------------- prices
def kraken_sol():
    try:
        r = http_json("https://api.kraken.com/0/public/Ticker?pair=SOLUSD")
        return float(r["result"]["SOLUSD"]["c"][0])
    except Exception:
        return None

def prices(mints):
    """mint -> {price, liq, symbol, source}"""
    out = {}
    mints = [m for m in mints if m not in STABLE and m != SOL]
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        for attempt in range(3):
            try:
                d = http_json("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(chunk))
                for p in d.get("pairs") or []:
                    if p.get("chainId") != "solana":
                        continue
                    a = p["baseToken"]["address"]
                    liq = float((p.get("liquidity") or {}).get("usd") or 0)
                    if a in chunk and p.get("priceUsd") and liq > out.get(a, {}).get("liq", -1):
                        out[a] = {"price": float(p["priceUsd"]), "liq": liq,
                                  "symbol": p["baseToken"].get("symbol"), "source": f"DexScreener {p.get('dexId')}"}
                break
            except Exception as e:
                log("dexscreener", type(e).__name__, str(e)[:80])
                time.sleep(3 + 3 * attempt)
    missing = [m for m in mints if m not in out]
    if missing:
        try:
            q = ",".join("solana:" + m for m in missing)
            d = http_json("https://coins.llama.fi/prices/current/" + q)
            for k, v in (d.get("coins") or {}).items():
                m = k.split(":", 1)[1]
                if v.get("confidence", 0) >= 0.9:
                    out[m] = {"price": float(v["price"]), "liq": None, "symbol": v.get("symbol"), "source": "DefiLlama"}
        except Exception as e:
            log("defillama", type(e).__name__)
    sol = kraken_sol()
    if sol:
        out[SOL] = {"price": sol, "liq": None, "symbol": "SOL", "source": "Kraken SOLUSD"}
    for m, s in STABLE.items():
        out[m] = {"price": 1.0, "liq": None, "symbol": s, "source": "par"}
    return out

# ---------------------------------------------------------------- chain
def balances():
    bal = {}
    for prog in TOKEN_PROGRAMS:
        r = rpc("getTokenAccountsByOwner", [OWNER, {"programId": prog}, {"encoding": "jsonParsed"}])
        for a in r["value"]:
            info = a["account"]["data"]["parsed"]["info"]
            amt = float(info["tokenAmount"]["uiAmountString"] or 0)
            if amt > 0:
                bal[info["mint"]] = bal.get(info["mint"], 0) + amt
    bal[SOL] = rpc("getBalance", [OWNER])["value"] / 1e9
    bal.setdefault(USDC, 0.0)
    return bal

def new_signatures(processed):
    out, before = [], None
    for _ in range(10):
        opts = {"limit": 100}
        if before:
            opts["before"] = before
        page = rpc("getSignaturesForAddress", [OWNER, opts])
        if not page:
            break
        for s in page:
            if s["signature"] in processed:
                return list(reversed(out))
            out.append(s)
        before = page[-1]["signature"]
    if processed:
        log("warning: no processed signature found in recent history; processing what was fetched")
    return list(reversed(out))

def tx_deltas(tx):
    def agg(k):
        d = {}
        for b in tx["meta"].get(k) or []:
            if b.get("owner") == OWNER:
                d[b["mint"]] = d.get(b["mint"], 0) + float(b["uiTokenAmount"]["uiAmountString"] or 0)
        return d
    pre, post = agg("preTokenBalances"), agg("postTokenBalances")
    deltas = {m: post.get(m, 0) - pre.get(m, 0) for m in set(pre) | set(post)}
    keys = [a["pubkey"] if isinstance(a, dict) else a for a in tx["transaction"]["message"]["accountKeys"]]
    if OWNER in keys:
        i = keys.index(OWNER)
        sol = (tx["meta"]["postBalances"][i] - tx["meta"]["preBalances"][i]) / 1e9
        if i == 0:
            sol += tx["meta"]["fee"] / 1e9
        if abs(sol) >= 0.01:          # ignore fees / rent
            deltas[SOL] = deltas.get(SOL, 0) + sol
    deltas = {m: d for m, d in deltas.items() if abs(d) > 1e-12}
    return deltas, pre

def transfer_counterparty(tx, mint):
    for ins in tx["transaction"]["message"]["instructions"]:
        p = ins.get("parsed") if isinstance(ins, dict) else None
        if isinstance(p, dict) and p.get("type") in ("transfer", "transferChecked"):
            return (p.get("info") or {}).get("destination")
    return None

# ---------------------------------------------------------------- core
def process(ledger, px, sym):
    processed = set(ledger["processed"])
    sigs = new_signatures(processed)
    changed = False
    for s in sigs:
        sig = s["signature"]
        if s.get("err"):
            ledger["processed"].append(sig); changed = True
            continue
        tx = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        time.sleep(0.4)
        if not tx or not tx.get("meta"):
            log("skip (not yet available)", sig[:10]); break   # retry next run, keep order
        t = datetime.datetime.fromtimestamp(tx["blockTime"], TZ).strftime("%Y-%m-%d %H:%M:%S")
        deltas, pre = tx_deltas(tx)
        ins = {m: d for m, d in deltas.items() if d > 0}
        outs = {m: -d for m, d in deltas.items() if d < 0}
        usd = lambda m, q: q * (px.get(m, {}).get("price") or 0)
        cost = ledger["cost"]
        entry = None
        if ins and outs:
            # swap
            approx = any(m not in STABLE for m in outs)
            spent = sum((q if m in STABLE else usd(m, q)) for m, q in outs.items())
            # sold legs -> realized
            for m, q in outs.items():
                if m in STABLE:
                    continue
                proceeds = sum(v for k, v in ins.items() if k in STABLE) if len(outs) == 1 else usd(m, q)
                if not any(k in STABLE for k in ins):
                    proceeds = usd(m, q)
                pre_q = pre.get(m, q) if m != SOL else None
                c = cost.get(m)
                cost_out = (c * q / pre_q) if (c is not None and pre_q) else None
                if c is not None and cost_out is not None:
                    cost[m] = max(c - cost_out, 0)
                if m != SOL:
                    ledger["realized"].append({"time": t, "sig": sig, "symbol": sym(m), "mint": m, "qty": q,
                        "proceeds": proceeds, "cost": cost_out,
                        "pnl": (proceeds - cost_out) if cost_out is not None else None,
                        "approx": not any(k in STABLE for k in ins)})
            # bought legs -> cost basis (split by market value if several)
            buys = {m: q for m, q in ins.items() if m not in STABLE}
            if buys:
                w = {m: usd(m, q) or 1 for m, q in buys.items()}
                tw = sum(w.values())
                for m, q in buys.items():
                    share = spent * w[m] / tw
                    cost[m] = (cost.get(m) or 0) + share
                    ledger["last_buy"][m] = {"time": t, "qty": q, "paid": share,
                        "paidToken": "/".join(sym(k) for k in outs), "tx": sig, "approx": approx}
            text = "Swap " + " + ".join(f"{fmt(q)} {sym(m)}" for m, q in outs.items()) + " → " + \
                   " + ".join(f"{fmt(q)} {sym(m)}" for m, q in ins.items())
            if buys and len(buys) == 1 and not approx:
                (m, q), = buys.items(); text += f" (~${spent / q:.6g}/{sym(m)})"
            entry = text
        elif ins:
            vals = sum(usd(m, q) for m, q in ins.items())
            if vals >= MIN_VALUE:
                entry = "Received " + " + ".join(f"{fmt(q)} {sym(m)}" for m, q in ins.items()) + " (zero cost)"
            elif any(m in STABLE for m in ins):
                entry = "Dust " + " + ".join(f"{fmt(q)} {sym(m)}" for m, q in ins.items()) + " (possible address poisoning — never copy the sender)"
        elif outs:
            for m, q in outs.items():
                c = cost.get(m); pre_q = pre.get(m)
                if c is not None and pre_q:
                    cost[m] = max(c - c * q / pre_q, 0)
            vals = sum(usd(m, q) for m, q in outs.items())
            if vals >= MIN_VALUE:
                dest = transfer_counterparty(tx, next(iter(outs)))
                entry = "Sent " + " + ".join(f"{fmt(q)} {sym(m)}" for m, q in outs.items()) + (f" → token acct {short(dest)}" if dest else "")
        if entry:
            ledger["activity"].append({"time": t, "sig": sig, "text": entry})
        ledger["processed"].append(sig)
        changed = True
    ledger["activity"] = ledger["activity"][-MAX_ACTIVITY:]
    ledger["processed"] = ledger["processed"][-5000:]
    return changed, len(sigs)

def build(ledger, bal, px):
    rows = []
    known = ledger["symbols"]
    order = sorted(bal, key=lambda m: -(bal[m] * (px.get(m, {}).get("price") or 0)))
    for m in order:
        q = bal[m]; p = px.get(m) or {}
        price = p.get("price")
        tracked = ledger["cost"].get(m) is not None or m in (USDC, SOL) or m in ledger.get("pinned", [])
        if not tracked:
            if price is None or q * price < MIN_VALUE:
                continue
            if p.get("liq") is not None and p["liq"] < MIN_LIQ:
                continue
            if p.get("liq") is None and p.get("source") != "DefiLlama":
                continue
        symbol = known.get(m) or p.get("symbol") or m[:4]
        c = ledger["cost"].get(m)
        row = {"id": "sol" if m == SOL else ("usdc" if m == USDC else m[:8].lower()),
               "symbol": symbol, "mint": m, "holdings": q,
               "mark": price if price is not None else 0,
               "avg": (c / q) if (c is not None and q > 0) else None}
        lb = ledger["last_buy"].get(m)
        if lb:
            row["lastBuy"] = lb
        rows.append(row)
    # SOL + USDC at the end
    rows.sort(key=lambda r: (r["mint"] in (SOL, USDC), -(r["holdings"] * r["mark"])))
    return rows

def fingerprint(rows, ledger):
    return json.dumps({"rows": [(r["mint"], round(r["holdings"], 6), None if r["avg"] is None else round(r["avg"], 10)) for r in rows],
                       "act": ledger["activity"][-5:], "rz": len(ledger["realized"])}, sort_keys=True)

def write_html(data):
    s = open(HTML).read()
    block = "/*DATA_START*/\nconst DATA = " + json.dumps(data, indent=1, ensure_ascii=False) + ";\n/*DATA_END*/"
    s2, n = re.subn(r"/\*DATA_START\*/.*?/\*DATA_END\*/", lambda _: block, s, flags=re.S)
    if n != 1:
        raise RuntimeError("DATA markers not found in wallet-5mef.html")
    open(HTML, "w").write(s2)

def git(*args, check=True):
    r = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"git {args[0]} failed: {r.stderr.strip()[:300]}")
    return r

def publish(msg):
    ident = ["-c", "user.name=New Bot", "-c", "user.email=bot@local"]
    cur = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if cur != "main":
        raise RuntimeError(f"repo is on branch {cur}, expected main; not publishing")
    git("add", "wallet-5mef.html")
    if not git("diff", "--cached", "--quiet", check=False).returncode:
        return {"published": False, "reason": "no diff"}
    git(*ident, "commit", "-q", "-m", msg)
    git("pull", "-q", "--rebase", "origin", "main")
    r = git("push", "-q", "origin", "main", check=False)
    if r.returncode == 0:
        return {"published": True, "via": "push", "commit": git("rev-parse", "HEAD").stdout.strip()}
    # protected main -> PR + merge
    br = "auto/5mef-" + now_pt().strftime("%Y%m%d-%H%M%S")
    git("push", "-q", "origin", f"HEAD:refs/heads/{br}")
    git("reset", "-q", "--hard", "origin/main")
    subprocess.run(["gh", "pr", "create", "-R", "MrPamperino/pnl-tracker", "--base", "main", "--head", br,
                    "--title", msg, "--body", "Automated by update_5mef_page.py"], check=True, capture_output=True, cwd=REPO)
    subprocess.run(["gh", "pr", "merge", br, "-R", "MrPamperino/pnl-tracker", "--merge", "--delete-branch"],
                   check=True, capture_output=True, cwd=REPO)
    git("pull", "-q", "--ff-only", "origin", "main")
    return {"published": True, "via": "pr", "branch": br, "commit": git("rev-parse", "HEAD").stdout.strip()}

def main():
    args = set(sys.argv[1:])
    dry = "--dry-run" in args
    ledger = json.load(open(LEDGER))
    for k, v in (("processed", []), ("cost", {}), ("realized", []), ("activity", []),
                 ("last_buy", {}), ("symbols", {}), ("pinned", [])):
        ledger.setdefault(k, v)
    if not dry and "--no-push" not in args:
        if git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main":
            git("pull", "-q", "--ff-only", "origin", "main", check=False)
    bal = balances()
    px = prices(list(bal.keys()) + list(ledger["cost"].keys()))
    for m, p in px.items():
        if p.get("symbol") and m not in ledger["symbols"] and (ledger["cost"].get(m) is not None or (p.get("liq") or 0) >= MIN_LIQ):
            ledger["symbols"][m] = p["symbol"]
    sym = lambda m: ledger["symbols"].get(m) or (px.get(m) or {}).get("symbol") or STABLE.get(m) or ("SOL" if m == SOL else m[:4] + "…")
    changed_tx, n_new = process(ledger, px, sym)
    # drop cost for tokens fully gone
    for m in list(ledger["cost"]):
        if bal.get(m, 0) <= 0:
            ledger["cost"].pop(m); ledger["last_buy"].pop(m, None)
    rows = build(ledger, bal, px)
    fp = fingerprint(rows, ledger)
    changed = fp != ledger.get("fingerprint") or "--refresh-marks" in args
    total = sum(r["holdings"] * r["mark"] for r in rows)
    tcost = sum(r["holdings"] * r["avg"] for r in rows if r["avg"] is not None)
    tval_c = sum(r["holdings"] * r["mark"] for r in rows if r["avg"] is not None)
    asof = now_pt().strftime("%Y-%m-%d %H:%M:%S PT")
    data = {"asOf": asof, "positions": rows, "activity": list(reversed(ledger["activity"])),
            "realized": ledger["realized"]}
    summary = {"new_signatures": n_new, "changed": changed, "total_value": round(total, 2),
               "tracked_cost": round(tcost, 2), "tracked_upnl": round(tval_c - tcost, 2),
               "rows": [(r["symbol"], round(r["holdings"], 6), r["mark"]) for r in rows]}
    if dry:
        print(json.dumps(summary)); return
    ledger["fingerprint"] = fp
    ledger["updated"] = asof
    if changed:
        write_html(data)
        snap = os.path.join(REPO, f"_pnl_snapshot_5mef_{now_pt():%Y%m%d}.json")
        prev = {}
        if os.path.exists(snap):
            try: prev = json.load(open(snap))
            except Exception: prev = {}
        prev.update({"as_of": asof, "wallet": OWNER, "rows": rows, "total_value": total, "tracked_cost": tcost,
                     "tracked_upnl": tval_c - tcost, "realized": ledger["realized"], "activity": ledger["activity"]})
        json.dump(prev, open(snap, "w"), indent=1)
        if "--no-push" not in args:
            summary["publish"] = publish(f"5Mef auto-update {asof}")
    json.dump(ledger, open(LEDGER, "w"), indent=1)
    print(json.dumps(summary))

if __name__ == "__main__":
    main()
