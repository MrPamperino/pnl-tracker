#!/usr/bin/env python3
"""Poll Telegram getUpdates and reply to /pnl /status /start /help for the linked DM."""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

STATE_PATH = Path("/workspace/pnl-tracker/wallet5mef_watch_state.json")
ANALYSIS_PATH = Path("/workspace/pnl-tracker/wallet5mef_analysis.json")
OFFSET_PATH = Path("/workspace/pnl-tracker/telegram_updates_offset.txt")
SECRETS = [
    Path("/home/box/sand-data/box-secrets.json"),
    Path("/home/box/agent-data/box-secrets.json"),
]
ZEC_ATA = "GrdBZSQsW9fL6tv91TwMc8iRcXGjQ4bT3JQEP1QBAQaH"
RPCS = [
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
]


def load_token() -> str:
    for p in SECRETS:
        if not p.exists():
            continue
        d = json.loads(p.read_text())
        tok = (d.get("card") or {}).get("TELEGRAM_BOT_TOKEN") or d.get("TELEGRAM_BOT_TOKEN")
        if tok:
            return tok
    raise RuntimeError("TELEGRAM_BOT_TOKEN missing")


def tg(token: str, method: str, data=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    if data is None:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch(url: str, retries: int = 4):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 PnLTracker", "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except Exception as e:
            last = e
            time.sleep(1.0 * (i + 1))
    raise last


def rpc(method: str, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last = None
    for url in RPCS:
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.load(r)
                if "error" in data:
                    last = data["error"]
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return data["result"]
            except Exception as e:
                last = e
                time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(last)


def ui_amount(res):
    ta = res.get("value") if isinstance(res, dict) and "value" in res else res
    if ta.get("uiAmount") is not None:
        return float(ta["uiAmount"])
    return float(ta["amount"]) / (10 ** int(ta.get("decimals") or 0))


def bal(ata: str) -> float:
    try:
        return ui_amount(rpc("getTokenAccountBalance", [ata, {"commitment": "confirmed"}]))
    except Exception as e:
        # Closed/empty ATA (e.g. TACZ sold out) — treat as zero
        if "could not find account" in str(e) or "-32602" in str(e):
            return 0.0
        raise


PAGE_URL = "https://mrpamperino.github.io/pnl-tracker/wallet-5mef.html"
CMD_LOG = Path("/workspace/pnl-tracker/_telegram_cmd_log.jsonl")
CORE = ["ZCAT", "ZEC", "BAG", "USDC", "SOL"]


def money(n: float) -> str:
    sign = "−" if n < 0 else ""
    n = abs(n)
    return f"{sign}${n:,.0f}" if n >= 1000 else f"{sign}${n:,.2f}"


def smoney(n: float) -> str:
    return ("+" if n >= 0 else "") + money(n)


def pct(n: float) -> str:
    return f"{n:+.1f}%"


def mark_fmt(p: float) -> str:
    if p >= 100:
        return f"${p:,.2f}"
    if p >= 1:
        return f"${p:,.4f}"
    return f"${p:.4g}"


def qty_fmt(q: float) -> str:
    return f"{q:,.0f}" if q >= 1000 else f"{q:,.4f}".rstrip("0").rstrip(".")


def now_pt() -> str:
    import datetime, zoneinfo
    return datetime.datetime.now(zoneinfo.ZoneInfo("Europe/Lisbon")).strftime("%Y-%m-%d %H:%M PT")


def page_data() -> dict:
    import re
    html = Path("/workspace/pnl-tracker/wallet-5mef.html").read_text()
    m = re.search(r"/\*DATA_START\*/\s*const DATA\s*=\s*(\{.*?\});\s*/\*DATA_END\*/", html, re.S)
    return json.loads(m.group(1))


def gather():
    """Holdings + cost from the same source as the 5Mef page (ledger + on-chain), live prices.
    Falls back to the page's DATA block when on-chain balances can't be read."""
    import sys
    sys.path.insert(0, "/workspace/pnl-tracker")
    import update_5mef_page as U
    ledger = json.loads(Path(U.LEDGER).read_text())
    for k, v in (("processed", []), ("cost", {}), ("realized", []), ("activity", []),
                 ("last_buy", {}), ("symbols", {}), ("pinned", []), ("unknown_cost", [])):
        ledger.setdefault(k, v)
    U.migrate_unknown_cost(ledger)          # in-memory only, never saved here
    notes = []
    try:
        bal = U.balances()
        bal_src = "on-chain"
    except Exception as e:
        d = page_data()
        bal = {p["mint"]: float(p["holdings"]) for p in d["positions"]}
        bal_src = f"página ({d.get('asOf')})"
        notes.append(f"saldos on-chain indisponíveis ({type(e).__name__}); uso a página")
    try:
        px = U.prices(list(bal) + list(ledger["cost"]))
    except Exception as e:
        px = {}
        notes.append(f"preços indisponíveis ({type(e).__name__})")
    rows = U.build(ledger, bal, px)
    return U, ledger, rows, px, bal_src, notes


def compute_pnl() -> str:
    U, ledger, rows, px, bal_src, notes = gather()
    lines = [f"5Mef PnL · {now_pt()}", ""]
    tot_val = tot_cost = tot_cval = 0.0
    no_px = []
    srcs = set()
    for r in rows:
        m, q, sym = r["mint"], float(r["holdings"]), r["symbol"]
        if q <= 0:
            continue
        p = (px.get(m) or {}).get("price")
        if p:
            srcs.add(((px.get(m) or {}).get("source") or "?").split()[0])
        c = None if m in ledger["unknown_cost"] else ledger["cost"].get(m)
        lb = ledger["last_buy"].get(m) or {}
        # "~" when a material part of the basis came from a non-stable leg valued at market
        approx = bool(lb.get("approx")) and c is not None and c > 0 and (lb.get("paid") or 0) / c >= 0.01
        if m in (U.USDC, U.USDT):
            lines.append(f"{sym} {q:,.2f} · {money(q)}")
            tot_val += q
            continue
        if not p:
            no_px.append(sym)
            line = f"{sym} {qty_fmt(q)} · sem preço"
        else:
            v = q * p
            tot_val += v
            line = f"{sym} {qty_fmt(q)} · {mark_fmt(p)} · {money(v)}"
            if (px.get(m) or {}).get("stale"):
                line += " (preço em cache)"
        if m == U.SOL:
            pass
        elif c is None:
            line += " · custo desconhecido"
        else:
            line += f" · custo {'~' if approx else ''}{money(c)}"
            if p:
                u = q * p - c
                tot_cost += c; tot_cval += q * p
                line += f" · uPnL {smoney(u)}" + (f" ({pct(u / c * 100)})" if c > 0 else "")
        lines.append(line)
    lines.append("")
    lines.append(f"Total{' (com preço)' if no_px else ''}: {money(tot_val)}")
    if tot_cost > 0:
        u = tot_cval - tot_cost
        lines.append(f"uPnL (custo conhecido {money(tot_cost)}): {smoney(u)} ({pct(u / tot_cost * 100)})")
    rz = [x["pnl"] for x in ledger["realized"] if x.get("pnl") is not None]
    if rz:
        lines.append(f"Realizado (ledger, custo conhecido): {smoney(sum(rz))}")
    if no_px:
        lines.append("Sem preço: " + ", ".join(no_px))
    lines.append("")
    lines.append(f"Saldos: {bal_src} · preços: {'/'.join(sorted(srcs)) or '—'}")
    for n in notes:
        lines.append("⚠️ " + n)
    lines.append(PAGE_URL)
    return "\n".join(lines)


def fallback_pnl(err: Exception) -> str:
    """Last resort: page snapshot (no network)."""
    try:
        d = page_data()
        lines = [f"5Mef PnL · snapshot da página ({d.get('asOf')})", f"⚠️ dados ao vivo falharam: {type(err).__name__}", ""]
        tot = 0.0
        for p in d["positions"]:
            q, mk = float(p["holdings"]), float(p.get("mark") or 0)
            if q <= 0:
                continue
            if mk:
                tot += q * mk
                line = f"{p['symbol']} {qty_fmt(q)} · {mark_fmt(mk)} · {money(q * mk)}"
            else:
                line = f"{p['symbol']} {qty_fmt(q)} · sem preço"
            if p.get("avg") is not None and mk:
                c = q * p["avg"]; line += f" · custo {money(c)} · uPnL {smoney(q * mk - c)}"
            lines.append(line)
        lines += ["", f"Total (com preço): {money(tot)}", PAGE_URL]
        return "\n".join(lines)
    except Exception as e2:
        return f"5Mef PnL indisponível agora ({type(err).__name__}; página: {type(e2).__name__}). Tenta /pnl daqui a 5 min.\n{PAGE_URL}"


def pnl_reply() -> str:
    try:
        return compute_pnl()
    except Exception as e:
        return fallback_pnl(e)


def status_text() -> str:
    state = json.loads(STATE_PATH.read_text())
    return (
        "5Mef watch OK\n"
        "Alerts: on\n"
        f"Last check: {state.get('last_checked_at') or state.get('lastChecked')}\n"
        f"Last result: {state.get('last_result')}\n"
        f"Watermark slot: {state.get('watermark_slot') or state.get('lastSlot')}\n"
        "\n"
        "Commands: /pnl  /status  /start"
    )


def help_text() -> str:
    return (
        "Frentrack PnL bot\n"
        "\n"
        "/pnl — live 5Mef PnL\n"
        "/status — wallet watch status\n"
        "/start — this help\n"
        "\n"
        "Also: push alerts on new 5Mef trades."
    )


def normalize_cmd(text: str) -> str:
    t = (text or "").strip().split()[0] if text else ""
    if "@" in t:
        t = t.split("@", 1)[0]
    return t.lower()


def _clean(s: str, token: str) -> str:
    return str(s).replace(token, "<token>") if token else str(s)


def send(token: str, chat_id: int, text: str, cmd: str) -> dict:
    """sendMessage with retry on 429/5xx; logs ok + message_id to _telegram_cmd_log.jsonl."""
    import datetime, zoneinfo, urllib.error
    res = {"cmd": cmd, "ok": False}
    for attempt in range(3):
        try:
            r = tg(token, "sendMessage", {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True})
            res = {"cmd": cmd, "ok": bool(r.get("ok")), "message_id": (r.get("result") or {}).get("message_id")}
            if not r.get("ok"):
                res["error"] = _clean(r.get("description"), token)
            break
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:300]
            except Exception:
                pass
            res = {"cmd": cmd, "ok": False, "error": _clean(f"HTTP {e.code} {body}", token)}
            if e.code == 429 or e.code >= 500:
                try:
                    ra = json.loads(body).get("parameters", {}).get("retry_after", 2)
                except Exception:
                    ra = 2
                time.sleep(min(float(ra), 15) + attempt)
                continue
            break
        except Exception as e:
            res = {"cmd": cmd, "ok": False, "error": _clean(f"{type(e).__name__}: {e}", token)}
            time.sleep(2 + 2 * attempt)
    res["at"] = datetime.datetime.now(zoneinfo.ZoneInfo("Europe/Lisbon")).isoformat(timespec="seconds")
    res["chars"] = len(text)
    res["preview"] = text[:60]
    try:
        with CMD_LOG.open("a") as f:
            f.write(json.dumps(res, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return res


def reply_for(cmd: str) -> str:
    if cmd in ("/pnl", "/mef", "/5mef"):
        return pnl_reply()
    if cmd in ("/status", "/watch"):
        return status_text()
    if cmd in ("/start", "/help"):
        return help_text()
    return f"Unknown command {cmd}\n\n" + help_text()


def chat_id_from_state() -> int:
    state = json.loads(STATE_PATH.read_text())
    return int((state.get("telegram") or {}).get("chat_id") or state.get("telegram_chat_id") or 0)


def main():
    import sys
    args = set(sys.argv[1:])
    if "--dry-run" in args:          # print the /pnl text; no token, no Telegram calls
        print(pnl_reply())
        return
    token = load_token()
    chat_id = chat_id_from_state()
    if not chat_id:
        print("NO_CHAT_ID")
        return
    if "--send-test" in args:        # send exactly one /pnl reply, print result (no token)
        print(json.dumps(send(token, chat_id, pnl_reply(), "/pnl (test)"), ensure_ascii=False))
        return

    offset = 0
    if OFFSET_PATH.exists():
        try:
            offset = int(OFFSET_PATH.read_text().strip() or "0")
        except ValueError:
            offset = 0

    upd = tg(token, f"getUpdates?offset={offset}&timeout=0")
    results = upd.get("result") or []
    handled = []
    max_id = offset - 1 if offset else -1

    for u in results:
        uid = u.get("update_id", 0)
        max_id = max(max_id, uid)
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") != chat_id:
            continue
        cmd = normalize_cmd(msg.get("text") or "")
        if not cmd.startswith("/"):
            continue
        try:
            text = reply_for(cmd)
        except Exception as e:
            text = f"Erro a processar {cmd}: {type(e).__name__}: {_clean(e, token)[:200]}"
        handled.append(send(token, chat_id, text, cmd))

    if max_id >= 0:
        new_offset = max_id + 1
        OFFSET_PATH.write_text(str(new_offset) + "\n")
        # acknowledge with offset so Telegram drops them
        tg(token, f"getUpdates?offset={new_offset}&timeout=0")

    print(json.dumps({"pending": len(results), "handled": [{k: h.get(k) for k in ("cmd", "ok", "message_id", "error")} for h in handled],
                      "offset": max_id + 1 if max_id >= 0 else offset}, ensure_ascii=False))


if __name__ == "__main__":
    main()
