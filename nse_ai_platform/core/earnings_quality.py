"""§2.7 Earnings Quality & Analyst Sentiment — beat/miss streak, consensus, upgrades, earnings date."""
from __future__ import annotations
from datetime import datetime
from core.models import MarketSnapshot

def _clip(x, lo=0.0, hi=100.0): return max(lo, min(hi, x))
def _scale(v, low, high):
    if high == low: return 50.0
    return _clip((v - low) / (high - low) * 100)

def score_earnings_quality_pillar(snapshot: MarketSnapshot) -> tuple:
    facts = []; subs = []
    bmh = snapshot.eps_beat_miss_history
    if bmh:
        weights = list(range(1, len(bmh)+1))
        ws = sum(bmh[i]*weights[i] for i in range(len(bmh)))
        mp = sum(weights)
        subs.append(_scale(ws/mp, -1, 1))
        beats = sum(1 for x in bmh if x > 0); misses = sum(1 for x in bmh if x < 0)
        facts.append(f"{beats} beat(s), {misses} miss(es) of last {len(bmh)} quarters")
    else:
        subs.append(50.0); facts.append("earnings history unavailable")
    rm = snapshot.analyst_recommendation_mean; ac = snapshot.analyst_count
    if rm is not None and ac > 0:
        rs = _scale(-(rm-1), -4, 0)
        cw = min(1.0, ac/20)
        subs.append(50 + (rs-50)*cw)
        lbl = {1:"Strong Buy",2:"Buy",3:"Hold",4:"Underperform",5:"Sell"}.get(round(rm), f"{rm:.1f}")
        facts.append(f"analyst consensus: {lbl} ({ac} analysts)")
    else:
        subs.append(50.0)
    u, d = snapshot.upgrades_90d, snapshot.downgrades_90d
    tot = u + d
    if tot > 0:
        subs.append(_scale(u-d, -tot, tot))
        facts.append(f"{u} upgrade(s) vs {d} downgrade(s) last 90d")
    else:
        subs.append(50.0)
    if snapshot.earnings_date_str:
        try:
            ed = datetime.fromisoformat(snapshot.earnings_date_str[:10])
            days = (ed - datetime.now()).days
            if 0 <= days <= 30:
                facts.append(f"⚠ EARNINGS IN {days} DAYS — elevated volatility risk")
        except Exception: pass
    return _clip(round(sum(subs)/len(subs), 1)), facts
