"""§2.3 Financial Health pillar — liquidity, debt trend, working-capital trend, interest coverage."""
from __future__ import annotations
from core.models import MarketSnapshot

def _clip(x, lo=0.0, hi=100.0): return max(lo, min(hi, x))
def _scale(v, low, high):
    if high == low: return 50.0
    return _clip((v - low) / (high - low) * 100)

def _trend_direction(history, min_periods=2):
    if len(history) < min_periods: return 0.0
    n = len(history); mean_x = (n-1)/2.0; mean_y = sum(history)/n
    if mean_y == 0: return 0.0
    num = sum((i-mean_x)*(history[i]-mean_y) for i in range(n))
    den = sum((i-mean_x)**2 for i in range(n))
    if den == 0: return 0.0
    return max(-1.0, min(1.0, (num/den)/abs(mean_y)*5))

def score_financial_health_pillar(snapshot: MarketSnapshot) -> tuple:
    facts = []
    cr = snapshot.current_ratio
    qr = snapshot.quick_ratio
    cr_score = _scale(cr, 0.8, 3.0) if cr is not None else 50.0
    qr_score = _scale(qr, 0.5, 2.0) if qr is not None else cr_score
    if cr is not None: facts.append(f"current ratio {cr:.2f}x")
    else: facts.append("current ratio unavailable")
    if qr is not None: facts.append(f"quick ratio {qr:.2f}x")
    liquidity = 0.6*cr_score + 0.4*qr_score

    debt_hist = snapshot.total_debt_history
    if len(debt_hist) >= 2:
        trend = _trend_direction(debt_hist)
        debt_trend_score = _scale(-trend, -1, 1)
        d = "falling" if trend < -0.05 else "rising" if trend > 0.05 else "stable"
        facts.append(f"debt trend {d}")
    else:
        debt_trend_score = 50.0

    ca, cl = snapshot.current_assets_history, snapshot.current_liab_history
    if len(ca) >= 2 and len(cl) >= 2 and len(ca) == len(cl):
        wc = [ca[i]-cl[i] for i in range(len(ca))]
        wt = _trend_direction(wc)
        wc_score = _scale(wt, -1, 1)
        wd = "improving" if wt > 0.05 else "deteriorating" if wt < -0.05 else "stable"
        facts.append(f"working capital {wd}")
    else:
        wc_score = 50.0

    ic = snapshot.interest_coverage
    if ic is not None:
        ic_score = _scale(max(0.0, ic), 1.5, 8.0)
        facts.append(f"interest coverage {ic:.1f}x")
    else:
        ic_score = 50.0

    score = round(0.30*liquidity + 0.25*debt_trend_score + 0.20*wc_score + 0.25*ic_score, 1)
    return _clip(score), facts
