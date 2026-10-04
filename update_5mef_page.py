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
import json, os, re, sys, time, subprocess, datetime, urllib.request, urllib.error, zoneinfo
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from address_labels import label_for, SPAM_MINTS

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

PRICE_CACHE = os.path.join(REPO, "_price_cache.json")
PRICE_TTL = 90          # seconds a cached price is reused (avoids hammering APIs -> 429)
PRICE_STALE_MAX = 3600  # last-resort: reuse a cached price up to 1h old, flagged stale

def _get_json_retry(url, tries=2, base=1.5):
    """GET with backoff; honours 429/5xx. Returns None on failure."""
    for i in range(tries):
        try:
            return http_json(url, timeout=15)
        except urllib.error.HTTPError as e:
            log("price", url.split("/")[2], "HTTP", e.code)
            if e.code not in (429, 500, 502, 503, 504) or i == tries - 1:
                return None
            ra = e.headers.get("Retry-After") if e.headers else None
            time.sleep(min(float(ra) if ra and ra.isdigit() else base * (2 ** i), 5))
        except Exception as e:
            log("price", url.split("/")[2], type(e).__name__)
            if i < tries - 1:
                time.sleep(base * (2 ** i))
    return None

def _load_cache():
    try:
        return json.load(open(PRICE_CACHE))
    except Exception:
        return {}

def prices(mints, use_cache=True):
    """mint -> {price, liq, symbol, source[, stale]}.
    Order: cache (<90s) -> Jupiter price v3 -> DexScreener -> DefiLlama -> stale cache (<1h)."""
    now = time.time()
    cache = _load_cache() if use_cache else {}
    out = {}
    mints = list(dict.fromkeys(m for m in mints if m not in STABLE and m != SOL))
    for m in mints:
        c = cache.get(m)
        if c and now - c.get("ts", 0) < PRICE_TTL and c.get("price"):
            out[m] = {k: c[k] for k in ("price", "liq", "symbol", "source") if k in c}
    # tokens with no price anywhere (e.g. dead pools) are skipped for 10 min instead of re-hitting every API
    todo = [m for m in mints if m not in out and not (cache.get(m, {}).get("miss") and now - cache[m].get("ts", 0) < 600)]
    # 1) Jupiter price v3 (has liquidity)
    for i in range(0, len(todo), 50):
        chunk = todo[i:i + 50]
        d = _get_json_retry("https://lite-api.jup.ag/price/v3?ids=" + ",".join(chunk))
        for m, v in (d or {}).items():
            if isinstance(v, dict) and v.get("usdPrice"):
                out[m] = {"price": float(v["usdPrice"]), "liq": float(v.get("liquidity") or 0),
                          "symbol": None, "source": "Jupiter"}
    # 2) DexScreener (best-liquidity Solana pair)
    missing = [m for m in todo if m not in out]
    for i in range(0, len(missing), 30):
        chunk = missing[i:i + 30]
        d = _get_json_retry("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(chunk), tries=2)
        for p in (d or {}).get("pairs") or []:
            if p.get("chainId") != "solana":
                continue
            a = p["baseToken"]["address"]
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if a in chunk and p.get("priceUsd") and liq > out.get(a, {}).get("liq", -1):
                out[a] = {"price": float(p["priceUsd"]), "liq": liq,
                          "symbol": p["baseToken"].get("symbol"), "source": f"DexScreener {p.get('dexId')}"}
    # 3) DefiLlama
    missing = [m for m in todo if m not in out]
    if missing:
        d = _get_json_retry("https://coins.llama.fi/prices/current/" + ",".join("solana:" + m for m in missing), tries=2)
        for k, v in ((d or {}).get("coins") or {}).items():
            m = k.split(":", 1)[1]
            if v.get("confidence", 0) >= 0.9:
                out[m] = {"price": float(v["price"]), "liq": None, "symbol": v.get("symbol"), "source": "DefiLlama"}
    # 4) stale cache as last resort
    for m in todo:
        if m not in out:
            c = cache.get(m)
            if not (c and c.get("price")):
                cache[m] = {"miss": True, "ts": now}
            elif now - c.get("ts", 0) < PRICE_STALE_MAX:
                out[m] = {k: c[k] for k in ("price", "liq", "symbol", "source") if k in c}
                out[m]["stale"] = True
    sol = None
    c = cache.get(SOL)
    if c and now - c.get("ts", 0) < PRICE_TTL:
        sol = c["price"]
    else:
        sol = kraken_sol()
        if not sol:
            d = _get_json_retry("https://lite-api.jup.ag/price/v3?ids=" + SOL, tries=2)
            sol = float(((d or {}).get(SOL) or {}).get("usdPrice") or 0) or None
        if not sol and c and now - c.get("ts", 0) < PRICE_STALE_MAX:
            sol = c["price"]
    if sol:
        out[SOL] = {"price": sol, "liq": None, "symbol": "SOL", "source": "Kraken SOLUSD"}
    # write fresh results back to cache
    try:
        for m in todo + [SOL]:
            v = out.get(m)
            if v and v.get("price") and not v.get("stale") and not (m == SOL and c and now - c.get("ts", 0) < PRICE_TTL):
                cache[m] = dict(v, ts=now)
        tmp = PRICE_CACHE + ".tmp"
        json.dump(cache, open(tmp, "w")); os.replace(tmp, PRICE_CACHE)
    except Exception:
        pass
    for m, sy in STABLE.items():
        out[m] = {"price": 1.0, "liq": None, "symbol": sy, "source": "par"}
    return out

# ---------------------------------------------------------------- chain
def balances():
    bal = {}
    for prog in TOKEN_PROGRAMS:
        r = rpc("getTokenAccountsByOwner", [OWNER, {"programId": prog}, {"encoding": "jsonParsed"}])
        for a in r["value"]:
            info = a["account"]["data"]["parsed"]["info"]
            amt = float(info["tokenAmount"]["uiAmountString"] or 0)
            if amt > 0 and info["mint"] not in SPAM_MINTS:   # fake tokens: full-mint match only
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
    deltas = {m: d for m, d in deltas.items() if abs(d) > 1e-12 and m not in SPAM_MINTS}
    return deltas, {m: q for m, q in pre.items() if m not in SPAM_MINTS}

def _acct_key(tx, b):
    k = tx["transaction"]["message"]["accountKeys"][b["accountIndex"]]
    return k["pubkey"] if isinstance(k, dict) else k

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
        unk = ledger.setdefault("unknown_cost", [])
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
                c = None if m in unk else cost.get(m)
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
                    # buying into a balance whose cost is unknown keeps the whole position unknown
                    if m in unk or (cost.get(m) is None and pre.get(m, 0) > 1e-12):
                        if m not in unk:
                            unk.append(m)
                        cost.pop(m, None)
                    else:
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
                lab = label_for(dest, *(b.get("owner") for b in (tx["meta"].get("postTokenBalances") or [])
                                        if dest and _acct_key(tx, b) == dest)) if dest else None
                entry = "Sent " + " + ".join(f"{fmt(q)} {sym(m)}" for m, q in outs.items()) + \
                        ((f" → {lab} (token acct {short(dest)})" if lab else f" → token acct {short(dest)}") if dest else "")
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
        if m in SPAM_MINTS:
            continue
        q = bal[m]; p = px.get(m) or {}
        price = p.get("price")
        tracked = (ledger["cost"].get(m) is not None or m in (USDC, SOL) or m in ledger.get("pinned", [])
                   or m in ledger.get("unknown_cost", []))
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
        if m in ledger.get("unknown_cost", []):
            row["costUnknown"] = True
        if p.get("stale"):
            row["markStale"] = True
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
    git("pull", "-q", "--rebase", "--autostash", "origin", "main")
    r = git("push", "-q", "origin", "main", check=False)
    if r.returncode == 0:
        return {"published": True, "via": "push", "commit": git("rev-parse", "HEAD").stdout.strip()}
    # protected main -> PR + merge
    br = "auto/5mef-" + now_pt().strftime("%Y%m%d-%H%M%S")
    git("push", "-q", "origin", f"HEAD:refs/heads/{br}")
    git("reset", "-q", "--keep", "origin/main")  # keeps unrelated local edits
    subprocess.run(["gh", "pr", "create", "-R", "MrPamperino/pnl-tracker", "--base", "main", "--head", br,
                    "--title", msg, "--body", "Automated by update_5mef_page.py"], check=True, capture_output=True, cwd=REPO)
    subprocess.run(["gh", "pr", "merge", br, "-R", "MrPamperino/pnl-tracker", "--merge", "--delete-branch"],
                   check=True, capture_output=True, cwd=REPO)
    git("pull", "-q", "--ff-only", "--autostash", "origin", "main", check=False)
    return {"published": True, "via": "pr", "branch": br, "commit": git("rev-parse", "HEAD").stdout.strip()}

ZEC_MINT = "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS"

def migrate_unknown_cost(ledger):
    """One-time fix: ZEC was held before the ledger started (cost unknown); a small buy
    had turned it into a fake known cost. Mark it unknown and null realized cost/pnl."""
    if ledger.get("migrations", {}).get("zec_unknown_cost"):
        return
    if ZEC_MINT not in ledger["unknown_cost"]:
        ledger["unknown_cost"].append(ZEC_MINT)
    ledger["cost"].pop(ZEC_MINT, None)
    for r in ledger["realized"]:
        if r.get("mint") == ZEC_MINT and r.get("cost") is not None:
            r["cost"] = None; r["pnl"] = None; r["note"] = "cost unknown (pre-ledger ZEC)"
    ledger.setdefault("migrations", {})["zec_unknown_cost"] = datetime.datetime.now(TZ).isoformat(timespec="seconds")

def main():
    args = set(sys.argv[1:])
    dry = "--dry-run" in args
    ledger = json.load(open(LEDGER))
    for k, v in (("processed", []), ("cost", {}), ("realized", []), ("activity", []),
                 ("last_buy", {}), ("symbols", {}), ("pinned", []), ("unknown_cost", [])):
        ledger.setdefault(k, v)
    migrate_unknown_cost(ledger)
    if not dry and "--no-push" not in args:
        if git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main":
            git("pull", "-q", "--rebase", "--autostash", "origin", "main", check=False)
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
    ledger["unknown_cost"] = [m for m in ledger["unknown_cost"] if bal.get(m, 0) > 0]
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
