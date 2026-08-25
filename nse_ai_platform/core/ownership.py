"""§2.6 Ownership & Governance — promoter trend, institutional trend, short interest."""
from __future__ import annotations
from core.models import MarketSnapshot

def _clip(x, lo=0.0, hi=100.0): return max(lo, min(hi, x))
def _scale(v, low, high):
    if high == low: return 50.0
    return _clip((v - low) / (high - low) * 100)

def score_ownership_pillar(snapshot: MarketSnapshot) -> tuple:
    facts = []; subs = []
    pnow = snapshot.promoter_holding_pct
    pprev = snapshot.promoter_holding_pct_prev
    subs.append(_scale(pnow, 20, 70))
    facts.append(f"promoter holding {pnow:.1f}%")
    if pprev is not None:
        delta = pnow - pprev
        subs.append(_scale(delta, -5.0, 3.0))
        d = "increased" if delta > 0.2 else "decreased" if delta < -0.2 else "stable"
        facts.append(f"promoter stake {d} ({delta:+.1f}pp QoQ)")
    else:
        subs.append(50.0)
    inst = snapshot.fii_holding_pct + snapshot.dii_holding_pct
    iprev = snapshot.institutional_pct_prev
    subs.append(_scale(inst, 5, 50))
    if iprev is not None:
        id_ = inst - iprev
        subs.append(_scale(id_, -5.0, 3.0))
        d2 = "increasing" if id_ > 0.2 else "decreasing" if id_ < -0.2 else "stable"
        facts.append(f"institutional {inst:.1f}% ({d2})")
    else:
        subs.append(50.0); facts.append(f"institutional {inst:.1f}%")
    sr = snapshot.shares_short_ratio
    if sr is not None:
        subs.append(_scale(-sr, -15, 0))
        facts.append(f"short interest {sr:.1f} days-to-cover")
    else:
        subs.append(50.0)
    return _clip(round(sum(subs)/len(subs), 1)), facts
