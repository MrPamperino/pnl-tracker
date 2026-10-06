"""Raydium CLMM + Orca Whirlpool position discovery/valuation for a Solana wallet (read-only, pure Python).

Position NFTs are the wallet's token accounts with decimals 0 and amount 1 (SPL Token and Token-2022).
For each NFT mint the position PDA (seeds ["position", mint]) is derived under each program; accounts that
exist and are owned by that program are positions. Token amounts are computed from liquidity + ticks +
the pool's current sqrt price (standard concentrated-liquidity math). Unclaimed fees = tokens_owed stored
in the position account (only refreshed on-chain when the position is touched -> a lower bound).
"""
import base64, hashlib, math, struct

RAYDIUM_CLMM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
ORCA_WHIRLPOOL = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
Q64 = 1 << 64

# ---------------------------------------------------------------- base58 / PDA (no external deps)
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def b58decode(s):
    n = 0
    for c in s:
        n = n * 58 + _B58.index(c)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + raw

def b58encode(b):
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out

_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)

def _on_curve(b):
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return True
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _I % _P
    return (x * x - x2) % _P == 0

def find_pda(seeds, program):
    prog = b58decode(program)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + prog + b"ProgramDerivedAddress").digest()
        if not _on_curve(h):
            return b58encode(h)
    raise ValueError("no PDA")

# ---------------------------------------------------------------- math
def _sqrt_x64(tick):
    return int(math.sqrt(1.0001 ** tick) * Q64)

def amounts(liq, sqrt_p, tick_lo, tick_hi):
    sa, sb = _sqrt_x64(tick_lo), _sqrt_x64(tick_hi)
    if sqrt_p <= sa:
        return liq * (sb - sa) * Q64 // (sa * sb), 0
    if sqrt_p >= sb:
        return 0, liq * (sb - sa) // Q64
    return liq * (sb - sqrt_p) * Q64 // (sqrt_p * sb), liq * (sqrt_p - sa) // Q64

def _pk(raw, off):
    return b58encode(raw[off:off + 32])

def _u128(raw, off):
    return int.from_bytes(raw[off:off + 16], "little")

# ---------------------------------------------------------------- discovery
def positions(rpc, nft_mints):
    """rpc(method, params) -> result. Returns list of position dicts (raw token amounts + decimals)."""
    if not nft_mints:
        return []
    cand = []   # (pda, mint, program)
    for m in nft_mints:
        mb = b58decode(m)
        cand.append((find_pda([b"position", mb], RAYDIUM_CLMM), m, RAYDIUM_CLMM))
        cand.append((find_pda([b"position", mb], ORCA_WHIRLPOOL), m, ORCA_WHIRLPOOL))
    accs = get_many(rpc, [c[0] for c in cand])
    found = []
    for (pda, mint, prog), a in zip(cand, accs):
        if not a or a.get("owner") != prog:
            continue
        raw = base64.b64decode(a["data"][0])
        if prog == RAYDIUM_CLMM:
            # PersonalPositionState: disc8 bump1 nft_mint32 pool_id32 tick_lower i32 tick_upper i32 liquidity u128
            #                        fee_growth_inside_0/1 u128 x2, token_fees_owed_0/1 u64 x2
            found.append({"protocol": "Raydium CLMM", "nft": mint, "pda": pda, "pool": _pk(raw, 41),
                          "tick_lo": struct.unpack_from("<i", raw, 73)[0], "tick_hi": struct.unpack_from("<i", raw, 77)[0],
                          "liq": _u128(raw, 81), "fee0": struct.unpack_from("<Q", raw, 129)[0], "fee1": struct.unpack_from("<Q", raw, 137)[0]})
        else:
            # Whirlpool Position: disc8 whirlpool32 position_mint32 liquidity u128 tick_lower i32 tick_upper i32
            #                     fee_growth_checkpoint_a/b u128 x2, fee_owed_a/b u64 x2
            found.append({"protocol": "Orca Whirlpool", "nft": mint, "pda": pda, "pool": _pk(raw, 8),
                          "tick_lo": struct.unpack_from("<i", raw, 88)[0], "tick_hi": struct.unpack_from("<i", raw, 92)[0],
                          "liq": _u128(raw, 72), "fee0": struct.unpack_from("<Q", raw, 128)[0], "fee1": struct.unpack_from("<Q", raw, 136)[0]})
    if not found:
        return []
    pools = sorted({p["pool"] for p in found})
    pacc = dict(zip(pools, get_many(rpc, pools)))
    pinfo, need_dec = {}, set()
    for pk, a in pacc.items():
        raw = base64.b64decode(a["data"][0])
        if a["owner"] == RAYDIUM_CLMM:
            # PoolState: disc8 bump1 amm_config32 owner32 mint0@73 mint1@105 vault0 vault1 observation dec0@233 dec1@234
            #            tick_spacing u16@235 liquidity u128@237 sqrt_price_x64 u128@253 tick_current i32@269
            pinfo[pk] = {"mint0": _pk(raw, 73), "mint1": _pk(raw, 105), "dec0": raw[233], "dec1": raw[234], "sqrt": _u128(raw, 253)}
        else:
            # Whirlpool: sqrt_price u128@65 tick_current i32@81 token_mint_a@101 token_mint_b@181
            pinfo[pk] = {"mint0": _pk(raw, 101), "mint1": _pk(raw, 181), "sqrt": _u128(raw, 65)}
            need_dec |= {pinfo[pk]["mint0"], pinfo[pk]["mint1"]}
    if need_dec:
        nd = sorted(need_dec)
        dec = {m: a["data"]["parsed"]["info"]["decimals"] for m, a in zip(nd, get_many(rpc, nd, parsed=True))}
        for p in pinfo.values():
            if "dec0" not in p:
                p["dec0"], p["dec1"] = dec[p["mint0"]], dec[p["mint1"]]
    out = []
    for p in found:
        pi = pinfo[p["pool"]]
        a0, a1 = amounts(p["liq"], pi["sqrt"], p["tick_lo"], p["tick_hi"])
        out.append({"protocol": p["protocol"], "nft": p["nft"], "pool": p["pool"],
                    "mint0": pi["mint0"], "mint1": pi["mint1"],
                    "amount0": a0 / 10 ** pi["dec0"], "amount1": a1 / 10 ** pi["dec1"],
                    "fee0": p["fee0"] / 10 ** pi["dec0"], "fee1": p["fee1"] / 10 ** pi["dec1"],
                    "in_range": _sqrt_x64(p["tick_lo"]) < pi["sqrt"] < _sqrt_x64(p["tick_hi"])})
    return out

def get_many(rpc, keys, parsed=False):
    res = []
    for i in range(0, len(keys), 100):
        r = rpc("getMultipleAccounts", [keys[i:i + 100], {"encoding": "jsonParsed" if parsed else "base64"}])
        res += r["value"]
    return res
