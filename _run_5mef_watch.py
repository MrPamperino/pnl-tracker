#!/usr/bin/env python3
"""One-shot 5Mef wallet watch: poll newer txs, alert Telegram, update state."""
import json, os, sys, time, subprocess, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from address_labels import label_for, party as addr_party, short as addr_short, SPAM_MINTS

STATE_PATH = "/workspace/pnl-tracker/wallet5mef_watch_state.json"
SECRETS_PATH = "/home/box/agent-data/box-secrets.json"
CACHE_DIR = "/workspace/pnl-tracker/_tx_cache"
REPORT_PATH = "/workspace/pnl-tracker/_watch_report.json"
SUMMARIES_PATH = "/workspace/pnl-tracker/_watch_summaries.json"
RAW_PATH = "/workspace/pnl-tracker/_watch_new_raw.json"
os.makedirs(CACHE_DIR, exist_ok=True)

STABLE = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "So11111111111111111111111111111111111111112",  # WSOL
}
STABLE_SYM = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "So11111111111111111111111111111111111111112": "SOL",
}
# Core holdings — swaps/sells of these get a loud ⚡ prefix so they don't drown in drip noise
MAJOR_MINTS = {
    "HcRLc9VDgjLeK154xDawfb1dmVJ98DoSqcwTHGqiDeJR",  # ZCAT
    "3nqHijNUExsnjNBb15WJsJ2xisyMVGN6FK4aUgZk1Rwj",  # TACZ
    "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS",  # ZEC
}
TG_LOG_PATH = "/workspace/pnl-tracker/_telegram_send_log.jsonl"

# USDC/USDT outflows at or above this are alerted as transfer_out (dust inflows stay suppressed)
USD_STABLES = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}
STABLE_OUT_ALERT_USD = 100.0

# Page auto-update (wallet-5mef.html) after activity
REPO_DIR = "/workspace/pnl-tracker"
PAGE_UPDATER = os.path.join(REPO_DIR, "update_5mef_page.py")
PAGE_UPDATE_TIMEOUT = 300

# CLI: --dry-run (no Telegram, no state/report writes, updater in --dry-run)
#      --replay-sigs SIG1,SIG2 (classify these signatures instead of polling; implies --dry-run)
DRY_RUN = "--dry-run" in sys.argv
REPLAY_SIGS = []
for _i, _a in enumerate(sys.argv):
    if _a == "--replay-sigs" and _i + 1 < len(sys.argv):
        REPLAY_SIGS = [x for x in sys.argv[_i + 1].split(",") if x]
        DRY_RUN = True

RPCS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
    "https://rpc.ankr.com/solana",
    "https://solana-mainnet.gateway.tatum.io",
]
rpc_idx = 0
rpc_errors = []

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def rpc(method, params, sleep=0.3, retries=6):
    global rpc_idx
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last_err = None
    for attempt in range(retries):
        url = RPCS[rpc_idx % len(RPCS)]
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json",
            "User-Agent": "pnl-tracker-watch/1.0",
        })
        try:
            time.sleep(sleep + 0.12 * attempt)
            with urllib.request.urlopen(req, timeout=45) as r:
                data = json.loads(r.read().decode())
            if "error" in data:
                err = str(data["error"])
                if "429" in err or "403" in err or "rate" in err.lower():
                    rpc_errors.append(f"{url}: {err[:80]}")
                    rpc_idx += 1
                    time.sleep(1.2 + attempt)
                    continue
                raise RuntimeError(err)
            return data["result"]
        except urllib.error.HTTPError as e:
            last_err = e
            rpc_errors.append(f"{url}: HTTP {e.code}")
            if e.code in (403, 413, 429, 502, 503):
                rpc_idx += 1
                time.sleep(1.5 + attempt)
                continue
            raise
        except Exception as e:
            last_err = e
            rpc_errors.append(f"{url}: {type(e).__name__}: {e}")
            rpc_idx += 1
            time.sleep(1.2 + attempt)
    raise RuntimeError(f"RPC failed after retries: {last_err}")

def get_tx(sig):
    path = os.path.join(CACHE_DIR, sig + ".json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    result = rpc("getTransaction", [sig, {
        "encoding": "jsonParsed",
        "maxSupportedTransactionVersion": 0,
        "commitment": "confirmed",
    }], sleep=0.35)
    with open(path, "w") as f:
        json.dump(result, f)
    return result

def get_newer_sigs(address, watermark_slot, watermark_sig, max_pages=5):
    """Return signatures newer than watermark (by slot, then exclude watermark sig)."""
    out = []
    before = None
    found_wm = False
    for _ in range(max_pages):
        params = [address, {"limit": 100}]
        if before:
            params[1]["before"] = before
        batch = rpc("getSignaturesForAddress", params, sleep=0.4)
        if not batch:
            break
        for s in batch:
            sig = s.get("signature")
            slot = s.get("slot") or 0
            if sig == watermark_sig:
                found_wm = True
                # stop once we hit watermark in this address's feed
                return out, found_wm
            if watermark_slot is not None and slot <= watermark_slot:
                # older or equal slot — if equal slot but different sig, keep only if strictly newer slot
                found_wm = True
                continue
            if s.get("err"):
                continue
            out.append({
                "signature": sig,
                "slot": slot,
                "blockTime": s.get("blockTime"),
            })
        before = batch[-1]["signature"]
        # if last batch item is at/below watermark, stop
        last_slot = batch[-1].get("slot") or 0
        if watermark_slot is not None and last_slot <= watermark_slot:
            found_wm = True
            break
        if len(batch) < 100:
            break
    return out, found_wm

def token_deltas(meta, owner):
    pre = defaultdict(float)
    post = defaultdict(float)
    for b in meta.get("preTokenBalances") or []:
        if b.get("owner") != owner:
            continue
        mint = b.get("mint")
        ui = (b.get("uiTokenAmount") or {}).get("uiAmount")
        if mint is None or ui is None:
            continue
        pre[mint] += float(ui)
    for b in meta.get("postTokenBalances") or []:
        if b.get("owner") != owner:
            continue
        mint = b.get("mint")
        ui = (b.get("uiTokenAmount") or {}).get("uiAmount")
        if mint is None or ui is None:
            continue
        post[mint] += float(ui)
    mints = set(pre) | set(post)
    deltas = {}
    for m in mints:
        d = post.get(m, 0.0) - pre.get(m, 0.0)
        if abs(d) > 1e-12:
            deltas[m] = d
    return deltas

def resolve_meta(mint):
    # Jupiter token list lite / DexScreener
    try:
        url = f"https://lite-api.jup.ag/tokens/v2/search?query={urllib.parse.quote(mint)}"
        req = urllib.request.Request(url, headers={"User-Agent": "pnl-tracker-watch/1.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read().decode())
        if isinstance(data, list):
            for t in data:
                if t.get("id") == mint or t.get("address") == mint:
                    return t.get("symbol") or mint[:6], t.get("name") or t.get("symbol") or mint[:8]
            if data:
                t = data[0]
                if mint in (t.get("id"), t.get("address")) or len(data) == 1:
                    return t.get("symbol") or mint[:6], t.get("name") or t.get("symbol") or mint[:8]
    except Exception:
        pass
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        req = urllib.request.Request(url, headers={"User-Agent": "pnl-tracker-watch/1.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read().decode())
        pairs = data.get("pairs") or []
        if pairs:
            base = pairs[0].get("baseToken") or {}
            if base.get("address") == mint:
                return base.get("symbol") or mint[:6], base.get("name") or base.get("symbol") or mint[:8]
            quote = pairs[0].get("quoteToken") or {}
            if quote.get("address") == mint:
                return quote.get("symbol") or mint[:6], quote.get("name") or quote.get("symbol") or mint[:8]
            return base.get("symbol") or mint[:6], base.get("name") or mint[:8]
    except Exception:
        pass
    return mint[:6], mint[:8]

def fmt_amt(x):
    ax = abs(x)
    if ax >= 1000:
        return f"{x:,.2f}"
    if ax >= 1:
        return f"{x:.4f}".rstrip("0").rstrip(".")
    return f"{x:.6f}".rstrip("0").rstrip(".")

def load_tg_token():
    with open(SECRETS_PATH) as f:
        d = json.load(f)
    card = d.get("card") or {}
    tok = card.get("TELEGRAM_BOT_TOKEN") or d.get("TELEGRAM_BOT_TOKEN")
    if not tok:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
    return tok

def send_telegram(token, chat_id, text, meta=None):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": False,
        "disable_notification": False,
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode())
    mid = ((resp.get("result") or {}).get("message_id"))
    truly_ok = bool(resp.get("ok")) and mid is not None
    # append-only delivery log so we can prove what went out
    try:
        entry = {
            "at": now_iso(),
            "chat_id": chat_id,
            "ok": truly_ok,
            "api_ok": bool(resp.get("ok")),
            "message_id": mid,
            "text": text,
        }
        if meta:
            entry.update(meta)
        with open(TG_LOG_PATH, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
    if not truly_ok:
        raise RuntimeError(f"telegram send not confirmed (api_ok={resp.get('ok')} message_id={mid})")
    return resp

def refresh_token_accounts(owner, known_mints):
    """Periodically add current holdings to known_mints (no alert)."""
    try:
        res = rpc("getTokenAccountsByOwner", [
            owner,
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
            {"encoding": "jsonParsed"},
        ], sleep=0.4)
    except Exception as e:
        rpc_errors.append(f"token_accounts: {e}")
        return []
    added = []
    now = now_iso()
    for item in res.get("value") or []:
        try:
            info = item["account"]["data"]["parsed"]["info"]
            mint = info["mint"]
            ata = item["pubkey"]
            if mint in known_mints:
                if not known_mints[mint].get("ata"):
                    known_mints[mint]["ata"] = ata
                continue
            sym, name = resolve_meta(mint)
            known_mints[mint] = {
                "symbol": sym,
                "name": name,
                "ata": ata,
                "first_seen": now,
            }
            added.append(mint)
        except Exception:
            continue
    return added

def transfer_counterparty(tx, owner, mint, direction="out"):
    """(token_account, owner_wallet) of the other side of a transfer of `mint`.
    direction 'out': owner sent it (authority == owner) -> destination.
    direction 'in' : owner received it (destination is owner's token account) -> source."""
    try:
        msg = tx["transaction"]["message"]
        keys = [a["pubkey"] if isinstance(a, dict) else a for a in msg["accountKeys"]]
        owners, mints = {}, {}
        for b in (tx["meta"].get("postTokenBalances") or []) + (tx["meta"].get("preTokenBalances") or []):
            owners[keys[b["accountIndex"]]] = b.get("owner")
            mints[keys[b["accountIndex"]]] = b.get("mint")
        mine = {a for a, o in owners.items() if o == owner}
        ixs = list(msg.get("instructions") or [])
        for g in tx["meta"].get("innerInstructions") or []:
            ixs += g.get("instructions") or []
        for ix in ixs:
            p = ix.get("parsed") if isinstance(ix, dict) else None
            if not isinstance(p, dict) or p.get("type") not in ("transfer", "transferChecked"):
                continue
            info = p.get("info") or {}
            src, dst = info.get("source"), info.get("destination")
            m = info.get("mint") or mints.get(src) or mints.get(dst)
            if m and m != mint:
                continue
            if direction == "out" and (info.get("authority") == owner or src in mine) and dst not in mine:
                return dst, owners.get(dst)
            if direction == "in" and dst in mine and src not in mine:
                return src, owners.get(src) or info.get("authority")
    except Exception:
        pass
    return None, None

def fmt_party(acct, wallet):
    """'Binance (depósito da 5Mef)' when labeled (exact match on wallet or token account), else short address."""
    if not (acct or wallet):
        return None
    return addr_party(wallet, acct)

def fmt_usd_amt(x):
    s_ = f"{abs(x):,.2f}"
    return s_[:-3] if s_.endswith(".00") else s_

def stable_out_destination(tx, owner, mint):
    """Owner wallet of the destination token account for an outgoing transfer of `mint`."""
    acct, wallet = transfer_counterparty(tx, owner, mint, "out")
    return wallet or acct

def classify(deltas):
    bought = [(m, d) for m, d in deltas.items() if d > 0]
    sold = [(m, d) for m, d in deltas.items() if d < 0]
    bought.sort(key=lambda x: -x[1])
    sold.sort(key=lambda x: x[1])  # most negative first
    if not bought and not sold:
        return "other_no_token_delta", bought, sold, False
    # meaningful trade/swap: has a non-stable buy OR sell of non-dust non-stable
    non_stable_buys = [(m, d) for m, d in bought if m not in STABLE]
    non_stable_sells = [(m, d) for m, d in sold if m not in STABLE]
    if non_stable_buys and (sold or len(bought) >= 1):
        # swap-like if both sides, else transfer_in
        if sold:
            return "swap", bought, sold, True
        return "transfer_in", bought, sold, True
    if non_stable_sells and bought:
        return "swap", bought, sold, True
    if non_stable_sells and not bought:
        return "transfer_out", bought, sold, True
    # USDC/USDT leaving the wallet (>= $100, nothing received) is meaningful
    big_stable_out = [(m, d) for m, d in sold if m in USD_STABLES and -d >= STABLE_OUT_ALERT_USD]
    if big_stable_out and not bought:
        return "transfer_out", bought, sold, True
    # only stable movement (incl. dust inflows) stays suppressed
    if bought or sold:
        return "stable_move", bought, sold, False
    return "other", bought, sold, False

def run_page_update(reason):
    """Run update_5mef_page.py; return its JSON summary or an error dict. Never raises."""
    try:
        if not os.path.exists(PAGE_UPDATER):
            return {"error": "updater missing", "reason": reason, "status": "error"}
        def g(*a):
            return subprocess.run(["git", "-C", REPO_DIR, *a], capture_output=True, text=True, timeout=60)
        br = g("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if br != "main" and not DRY_RUN:
            if g("diff", "--quiet").returncode or g("diff", "--cached", "--quiet").returncode:
                return {"error": f"repo on {br} with uncommitted tracked changes; skipped", "reason": reason}
            co = g("checkout", "-q", "main")
            if co.returncode:
                return {"error": f"checkout main failed: {co.stderr.strip()[:200]}", "reason": reason}
        cmd = [sys.executable, PAGE_UPDATER] + (["--dry-run"] if DRY_RUN else [])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=PAGE_UPDATE_TIMEOUT, cwd=REPO_DIR)
        last = (r.stdout.strip().splitlines() or [""])[-1]
        try:
            out = json.loads(last)
        except Exception:
            out = {"error": f"exit {r.returncode}", "stderr_tail": r.stderr.strip()[-300:]}
        out["reason"] = reason
        pub = out.get("publish") or {}
        out["status"] = (pub.get("commit") if pub.get("published") else
                         ("error" if "error" in out else ("dry-run" if DRY_RUN else "no change")))
        return out
    except subprocess.TimeoutExpired:
        return {"error": f"timeout after {PAGE_UPDATE_TIMEOUT}s", "reason": reason, "status": "error"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "reason": reason, "status": "error"}

def main():
    with open(STATE_PATH) as f:
        state = json.load(f)

    owner = state["wallet"]
    wm_sig = state.get("watermark_signature") or state.get("lastSignature")
    wm_slot = state.get("watermark_slot") or state.get("lastSlot")
    known = state.setdefault("known_mints", {})
    chat_id = (state.get("telegram") or {}).get("chat_id") or state.get("telegram_chat_id")

    addrs = [owner]
    for k in ("zcat_ata", "tacz_ata"):
        if state.get(k):
            addrs.append(state[k])
    # Poll every known ATA: public RPC owner feeds often lag, so token
    # receives/swaps show up on ATAs first (incl. spam + new mints).
    for m, info in list(known.items()):
        ata = (info or {}).get("ata")
        if m in SPAM_MINTS:          # don't poll fake-token ATAs
            continue
        if ata and ata not in addrs:
            addrs.append(ata)

    # collect newer sigs across addresses
    by_sig = {}
    found_any = False
    for addr in ([] if REPLAY_SIGS else addrs):   # replay: no polling, only the given sigs
        newer, found = get_newer_sigs(addr, wm_slot, wm_sig)
        found_any = found_any or found
        for s in newer:
            prev = by_sig.get(s["signature"])
            if not prev or (s.get("slot") or 0) > (prev.get("slot") or 0):
                by_sig[s["signature"]] = s

    # Also filter: only slots strictly greater than watermark
    new_sigs = [s for s in by_sig.values() if wm_slot is None or (s.get("slot") or 0) > wm_slot]
    if REPLAY_SIGS:
        new_sigs = [{"signature": x, "slot": 0, "blockTime": None} for x in REPLAY_SIGS]
    new_sigs.sort(key=lambda x: (x.get("slot") or 0, x.get("blockTime") or 0))

    if not DRY_RUN:
        with open(RAW_PATH, "w") as f:
            json.dump({"found_watermark": found_any, "count": len(new_sigs), "sigs": new_sigs}, f, indent=2)

    summaries = []
    new_token_mints = []
    tg_messages = []
    max_slot = wm_slot or 0
    max_sig = wm_sig
    max_bt = state.get("watermark_blockTime")

    for s in new_sigs:
        sig = s["signature"]
        slot = s.get("slot") or 0
        bt = s.get("blockTime")
        if slot > max_slot:
            max_slot = slot
            max_sig = sig
            max_bt = bt
        elif slot == max_slot:
            max_sig = sig
            max_bt = bt

        try:
            tx = get_tx(sig)
        except Exception as e:
            summaries.append({
                "signature": sig, "slot": slot, "blockTime": bt,
                "classification": f"fetch_error:{type(e).__name__}",
                "meaningful": False, "is_new_token": False,
                "bought": [], "sold": [], "main_buy": None, "line": f"error {e}",
            })
            continue
        if not tx or not tx.get("meta"):
            summaries.append({
                "signature": sig, "slot": slot, "blockTime": bt,
                "classification": "empty_tx", "meaningful": False,
                "is_new_token": False, "bought": [], "sold": [],
                "main_buy": None, "line": "empty_tx",
            })
            continue

        deltas = token_deltas(tx["meta"], owner)
        spam_hit = sorted(m for m in deltas if m in SPAM_MINTS)   # full-mint match only, never by symbol
        deltas = {m: d for m, d in deltas.items() if m not in SPAM_MINTS}
        cls, bought, sold, meaningful = classify(deltas)
        if spam_hit and not deltas:
            cls = "spam"

        bought_info = []
        is_new_token = False
        main_buy = None
        for mint, amt in bought:
            entry = known.get(mint)
            was_known = mint in known
            if not was_known:
                sym, name = resolve_meta(mint)
                known[mint] = {
                    "symbol": sym,
                    "name": name,
                    "first_seen": now_iso(),
                }
                entry = known[mint]
                if mint not in STABLE:
                    is_new_token = True
                    new_token_mints.append(mint)
            else:
                sym = entry.get("symbol") or mint[:6]
                name = entry.get("name") or sym
            bi = {"mint": mint, "amount": amt, "symbol": entry.get("symbol") or sym, "name": entry.get("name") or name, "new": not was_known}
            bought_info.append(bi)
            if mint not in STABLE and (main_buy is None or amt > main_buy["amount"]):
                main_buy = bi

        sold_info = []
        for mint, amt in sold:
            entry = known.get(mint) or {}
            if mint not in known and mint not in STABLE:
                sym, name = resolve_meta(mint)
                known[mint] = {"symbol": sym, "name": name, "first_seen": now_iso()}
                entry = known[mint]
            sold_info.append({
                "mint": mint, "amount": amt,
                "symbol": entry.get("symbol") or mint[:6],
                "name": entry.get("name") or entry.get("symbol") or mint[:6],
            })

        # build line
        if main_buy:
            line = f"{'🆕 NEW TOKEN ' if is_new_token else ''}{cls}: +{fmt_amt(main_buy['amount'])} {main_buy['symbol']}"
            if sold_info:
                top_sell = max(sold_info, key=lambda x: abs(x["amount"]))
                line += f" / {fmt_amt(top_sell['amount'])} {top_sell['symbol']}"
        elif sold_info:
            top_sell = max(sold_info, key=lambda x: abs(x["amount"]))
            sym_ = STABLE_SYM.get(top_sell["mint"], top_sell["symbol"])
            line = f"{cls}: {fmt_amt(top_sell['amount'])} {sym_}"
        else:
            line = cls

        # counterparty (label only via exact full-address match)
        cp = None
        if cls in ("transfer_out", "transfer_in", "stable_move") and (sold_info or bought_info):
            if sold_info and not bought_info:
                top = max(sold_info, key=lambda x: abs(x["amount"])); direction = "out"
            elif bought_info and not sold_info:
                top = max(bought_info, key=lambda x: abs(x["amount"])); direction = "in"
            else:
                top = None
            if top:
                acct, wallet = transfer_counterparty(tx, owner, top["mint"], direction)
                if acct or wallet:
                    cp = {"direction": direction, "token_account": acct, "wallet": wallet,
                          "label": label_for(wallet, acct)}
                    if cls != "stable_move" or cp["label"]:
                        line += (" → " if direction == "out" else " ← ") + fmt_party(acct, wallet)
        summaries.append({
            "signature": sig, "slot": slot, "blockTime": bt,
            "counterparty": cp,
            "spam_ignored": spam_hit,
            "classification": cls, "meaningful": meaningful or is_new_token,
            "is_new_token": is_new_token,
            "bought": bought_info, "sold": sold_info,
            "main_buy": main_buy, "line": line,
        })

        if meaningful or is_new_token:
            # Telegram short message — swaps of major holdings get a loud prefix
            # so they don't drown in ZCAT drip noise.
            stable_buys = [
                {"mint": m, "amount": d, "symbol": STABLE_SYM.get(m, m[:4])}
                for m, d in bought if m in STABLE
            ]
            is_major = any(
                (bi.get("mint") in MAJOR_MINTS) for bi in bought_info
            ) or any((si.get("mint") in MAJOR_MINTS) for si in sold_info)
            is_swapish = cls in ("swap", "transfer_out") or (sold_info and (main_buy or stable_buys))

            if main_buy:
                mint = main_buy["mint"]
                dex = f"https://dexscreener.com/solana/{mint}"
                if is_new_token:
                    msg = f"🆕 NEW TOKEN\n+{fmt_amt(main_buy['amount'])} {main_buy['symbol']}"
                    if sold_info:
                        top_sell = max(sold_info, key=lambda x: abs(x["amount"]))
                        msg += f" for {fmt_amt(abs(top_sell['amount']))} {top_sell['symbol']}"
                    msg += f"\n{dex}"
                else:
                    # e.g. +2.19 ZEC for 3,426 USDC
                    if sold_info:
                        top_sell = max(sold_info, key=lambda x: abs(x["amount"]))
                        sold_sym = top_sell["symbol"]
                        if top_sell["mint"] in STABLE:
                            sold_sym = STABLE_SYM.get(top_sell["mint"], sold_sym)
                        body = f"+{fmt_amt(main_buy['amount'])} {main_buy['symbol']} ← {fmt_amt(abs(top_sell['amount']))} {sold_sym}"
                    else:
                        body = f"+{fmt_amt(main_buy['amount'])} {main_buy['symbol']}"
                        if cls == "transfer_in" and cp and cp["direction"] == "in":
                            body = f"↙️ {body} ← {fmt_party(cp['token_account'], cp['wallet'])}"
                    if is_swapish and is_major:
                        msg = f"⚡ SWAP\n{body}\n{dex}"
                    else:
                        msg = f"{body}\n{dex}"
            elif sold_info and cls == "transfer_out" and max(sold_info, key=lambda x: abs(x["amount"]))["mint"] in USD_STABLES:
                top_sell = max(sold_info, key=lambda x: abs(x["amount"]))
                acct, wallet = transfer_counterparty(tx, owner, top_sell["mint"], "out")
                sym_ = STABLE_SYM.get(top_sell["mint"], "USD")
                head = f"↗️ −{fmt_usd_amt(top_sell['amount'])} {sym_}"
                if acct or wallet:
                    lab = label_for(wallet, acct)
                    head += f" → {lab}" if lab else f" → {addr_short(wallet or acct)} (sem etiqueta)"
                    where = f"\nPara: {addr_short(wallet)}" if wallet else ""
                    where += f" (conta {sym_} {addr_short(acct)})" if acct and wallet else (f"\nConta {sym_}: {addr_short(acct)}" if acct else "")
                else:
                    head += " → destino desconhecido"; where = ""
                msg = f"📤 Transferência de saída\n{head}{where}\nhttps://solscan.io/tx/{sig}"
            elif sold_info:
                top_sell = max(sold_info, key=lambda x: abs(x["amount"]))
                mint = top_sell["mint"]
                dex = f"https://dexscreener.com/solana/{mint}"
                # TACZ→USDC used to be only "Sold TACZ" — easy to miss. Always show proceeds.
                if stable_buys:
                    top_buy = max(stable_buys, key=lambda x: abs(x["amount"]))
                    body = f"Sold {fmt_amt(abs(top_sell['amount']))} {top_sell['symbol']} → +{fmt_amt(top_buy['amount'])} {top_buy['symbol']}"
                elif cls == "transfer_out" and cp and cp["direction"] == "out":
                    body = f"↗️ −{fmt_amt(abs(top_sell['amount']))} {top_sell['symbol']} → {fmt_party(cp['token_account'], cp['wallet'])}"
                else:
                    body = f"Sold {fmt_amt(abs(top_sell['amount']))} {top_sell['symbol']}"
                if is_major:
                    msg = f"⚡ SWAP\n{body}\n{dex}"
                else:
                    msg = f"{body}\n{dex}"
            else:
                msg = line
            tg_messages.append({
                "sig": sig,
                "text": msg,
                "is_new_token": is_new_token,
                "is_major_swap": bool(is_swapish and is_major),
            })

    # periodic known_mints refresh from token accounts (every run is fine; cheap)
    refresh_token_accounts(owner, known)

    # advance watermark
    checked = now_iso()
    meaningful_trades = [s for s in summaries if s.get("meaningful")]
    result = "NONE"
    if meaningful_trades:
        if any(s.get("is_new_token") for s in meaningful_trades):
            result = "NEW_TOKEN"
        else:
            result = "TRADE"

    if new_sigs:
        state["watermark_signature"] = max_sig
        state["watermark_slot"] = max_slot
        state["watermark_blockTime"] = max_bt
        state["lastSignature"] = max_sig
        state["lastSlot"] = max_slot
    state["last_checked_at"] = checked
    state["lastChecked"] = checked
    state["last_new_count"] = len(meaningful_trades)
    state["last_result"] = result
    state["known_mints"] = known
    state["first_run"] = False

    if not DRY_RUN:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
            f.write("\n")

    tg_ok = []
    tg_err = None
    if tg_messages and chat_id and not DRY_RUN:
        try:
            token = load_tg_token()
            # one message per meaningful trade; batch only spam — we already filtered meaningful
            for m in tg_messages:
                resp = send_telegram(token, chat_id, m["text"], meta={
                    "sig": m["sig"],
                    "is_new_token": m["is_new_token"],
                    "is_major_swap": m.get("is_major_swap"),
                })
                mid = ((resp.get("result") or {}).get("message_id"))
                tg_ok.append({
                    "sig": m["sig"],
                    "ok": bool(resp.get("ok")) and mid is not None,
                    "message_id": mid,
                    "is_new_token": m["is_new_token"],
                    "is_major_swap": m.get("is_major_swap"),
                    "text": m["text"],
                })
                time.sleep(0.35)
        except Exception as e:
            tg_err = f"{type(e).__name__}: {e}"

    report = {
        "result": result,
        "new_signature_count": len(new_sigs),
        "meaningful_trades": len(meaningful_trades),
        "new_tokens": new_token_mints,
        "watermark_before": wm_sig,
        "watermark_after": state.get("watermark_signature"),
        "summaries": summaries,
        "telegram_sent": tg_ok,
        "telegram_error": tg_err,
        "rpc_errors_tail": rpc_errors[-10:],
    }
    # Page auto-update: after alerts are sent, never allowed to break the watch.
    lagged = (not found_any) or any(("429" in e or "403" in e) for e in rpc_errors)
    if new_sigs or lagged:
        report["page_update"] = run_page_update(reason="new_signatures" if new_sigs else "rpc_lag")
    else:
        report["page_update"] = {"skipped": "no activity"}
    report["dry_run"] = DRY_RUN
    report_path = REPORT_PATH if not DRY_RUN else REPORT_PATH.replace(".json", ".dryrun.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    if not DRY_RUN:
        with open(SUMMARIES_PATH, "w") as f:
            json.dump(summaries, f, indent=2)

    # stdout summary for automation (no secrets)
    confirmed = [x for x in tg_ok if x.get("ok") and x.get("message_id") is not None]
    print(json.dumps({
        "result": result,
        "new_signature_count": len(new_sigs),
        "meaningful_trades": len(meaningful_trades),
        "new_tokens": new_token_mints,
        "telegram_sent_count": len(tg_ok),
        "telegram_confirmed_count": len(confirmed),
        "telegram_message_ids": [x.get("message_id") for x in confirmed],
        "telegram_error": tg_err,
        "lines": [s["line"] for s in meaningful_trades],
        "tg_texts": [m["text"] for m in tg_messages],
        "major_swaps": sum(1 for m in tg_messages if m.get("is_major_swap")),
        "page_update": report.get("page_update"),
        "dry_run": DRY_RUN,
    }, indent=2))

if __name__ == "__main__":
    main()
