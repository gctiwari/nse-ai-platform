"""§4 — Checklist-based category classification replacing pure score thresholds."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from core.models import MarketSnapshot, Category, ScoreBreakdown
from core.disqualifiers import DisqualifierResult

@dataclass
class ClassificationThresholds:
    min_overall_score: float = 50.0
    min_confidence_score: float = 45.0
    min_risk_reward_ratio: float = 1.5
    watchlist_min_margin_of_safety_pct: float = 3.0

DEFAULT_THRESHOLDS = ClassificationThresholds()
MAX_PER_CATEGORY = 10

@dataclass
class ChecklistResult:
    category: Optional[Category]
    passed: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    notes: list = field(default_factory=list)

def _conservative_score(snap, scores):
    passed=[]; failed=[]
    if snap.is_financial_sector: passed.append("leverage waived (financial sector)")
    elif snap.debt_to_equity<=1.5: passed.append(f"D/E {snap.debt_to_equity:.2f}x ≤1.5x")
    else: failed.append(f"D/E {snap.debt_to_equity:.2f}x >1.5x")
    ic=snap.interest_coverage
    if ic is None: passed.append("interest coverage N/A")
    elif ic>=3.0: passed.append(f"interest coverage {ic:.1f}x")
    else: failed.append(f"interest coverage {ic:.1f}x <3x")
    if snap.roe_pct>=12: passed.append(f"ROE {snap.roe_pct:.1f}% ≥12%")
    else: failed.append(f"ROE {snap.roe_pct:.1f}% <12%")
    fcf=snap.fcf_history_annual[-3:] if snap.fcf_history_annual else []
    if not fcf: passed.append("FCF N/A")
    elif sum(1 for v in fcf if v>0)>=2: passed.append(f"FCF positive {sum(1 for v in fcf if v>0)}/{len(fcf)} yrs")
    else: failed.append(f"FCF positive only {sum(1 for v in fcf if v>0)}/{len(fcf)} yrs")
    rev=snap.revenue_history_annual[-4:]
    if len(rev)>=2:
        pos=sum(1 for i in range(1,len(rev)) if rev[i]>rev[i-1]); steps=len(rev)-1
        if pos>=max(1,steps-1): passed.append(f"revenue growing {pos}/{steps} yrs")
        else: failed.append(f"revenue growing {pos}/{steps} yrs")
    else: passed.append("revenue history N/A")
    cr=snap.current_ratio
    if cr is None: passed.append("current ratio N/A")
    elif cr>=1.2: passed.append(f"current ratio {cr:.2f}x")
    else: failed.append(f"current ratio {cr:.2f}x <1.2x")
    if snap.promoter_holding_pct>=35: passed.append(f"promoter {snap.promoter_holding_pct:.1f}%")
    else: failed.append(f"promoter {snap.promoter_holding_pct:.1f}% <35%")
    if scores.profitability_score>=50: passed.append(f"profitability {scores.profitability_score:.0f}")
    else: failed.append(f"profitability {scores.profitability_score:.0f} <50")
    return len(passed), passed, failed

def _balanced_score(snap, scores):
    passed=[]; failed=[]
    if snap.is_financial_sector: passed.append("leverage waived (financial sector)")
    elif snap.debt_to_equity<=2.5: passed.append(f"D/E {snap.debt_to_equity:.2f}x ≤2.5x")
    else: failed.append(f"D/E {snap.debt_to_equity:.2f}x >2.5x")
    ic=snap.interest_coverage
    if ic is None: passed.append("interest coverage N/A")
    elif ic>=1.5: passed.append(f"interest coverage {ic:.1f}x")
    else: failed.append(f"interest coverage {ic:.1f}x <1.5x")
    if snap.roe_pct>=8: passed.append(f"ROE {snap.roe_pct:.1f}%")
    else: failed.append(f"ROE {snap.roe_pct:.1f}% <8%")
    fcf=snap.fcf_history_annual[-3:] if snap.fcf_history_annual else []
    if not fcf: passed.append("FCF N/A")
    elif sum(1 for v in fcf if v>0)>=1: passed.append("FCF positive ≥1 yr")
    else: failed.append("FCF negative all yrs")
    fh=scores.financial_health_score
    if fh>=45: passed.append(f"financial health {fh:.0f}")
    else: failed.append(f"financial health {fh:.0f} <45")
    if scores.growth_score>=45: passed.append(f"growth {scores.growth_score:.0f}")
    else: failed.append(f"growth {scores.growth_score:.0f} <45")
    return len(passed), passed, failed

def classify(snap:MarketSnapshot, scores:ScoreBreakdown, fair_value:float,
             disq:DisqualifierResult, risk_reward_ratio:float,
             thresholds:ClassificationThresholds=DEFAULT_THRESHOLDS)->ChecklistResult:
    if disq.insufficient_data:
        return ChecklistResult(None,notes=["Insufficient data"])
    if disq.is_disqualified and not disq.aggressive_carveout_eligible:
        return ChecklistResult(None,notes=["Disqualified: "+"; ".join(disq.reasons)])
    if scores.overall_ai_score < thresholds.min_overall_score:
        return ChecklistResult(None,notes=[f"Score {scores.overall_ai_score:.1f} < {thresholds.min_overall_score}"])
    if scores.confidence_score < thresholds.min_confidence_score:
        return ChecklistResult(None,notes=[f"Confidence {scores.confidence_score:.1f} < {thresholds.min_confidence_score}"])
    if risk_reward_ratio < thresholds.min_risk_reward_ratio:
        return ChecklistResult(None,notes=[f"R:R {risk_reward_ratio:.2f} < {thresholds.min_risk_reward_ratio}"])
    if disq.aggressive_carveout_eligible:
        return ChecklistResult(Category.AGGRESSIVE,notes=["FCF carve-out → Aggressive only"])
    mos = (fair_value-snap.price)/fair_value*100 if fair_value else 0
    cp,cpassed,cfailed = _conservative_score(snap,scores)
    if cp>=6:
        if mos<thresholds.watchlist_min_margin_of_safety_pct:
            return ChecklistResult(Category.WATCHLIST,passed=cpassed,notes=[f"Conservative quality but MoS {mos:.1f}% insufficient"])
        return ChecklistResult(Category.CONSERVATIVE,passed=cpassed,failed=cfailed)
    bp,bpassed,bfailed = _balanced_score(snap,scores)
    if bp>=4:
        if mos<thresholds.watchlist_min_margin_of_safety_pct and cp>=5:
            return ChecklistResult(Category.WATCHLIST,passed=bpassed,notes=[f"Near-Conservative, MoS {mos:.1f}% too small"])
        return ChecklistResult(Category.BALANCED,passed=bpassed,failed=bfailed)
    return ChecklistResult(Category.AGGRESSIVE,failed=bfailed,notes=["Cleared gates, doesn't meet Balanced checklist"])
