"""§2.5 Valuation — cross-sectional sector stats + analyst targets."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Sequence
from core.models import MarketSnapshot

def _clip(x,lo=0.0,hi=100.0): return max(lo,min(hi,x))
def _scale(v,low,high):
    if high==low: return 50.0
    return _clip((v-low)/(high-low)*100)
def _median(values):
    clean=sorted(v for v in values if v is not None and v>0)
    if not clean: return None
    n=len(clean); mid=n//2
    return clean[mid] if n%2 else (clean[mid-1]+clean[mid])/2.0

@dataclass
class SectorStats:
    sector: str; median_pe: float|None; median_pb: float|None
    median_ev_ebitda: float|None; median_fcf_yield: float|None
    median_roe: float|None; stock_count: int

UNDERVALUED="Undervalued"; FAIRLY_VALUED="Fairly Valued"; OVERVALUED="Overvalued"

def compute_sector_stats(snapshots:Sequence[MarketSnapshot])->dict:
    by_sector:dict={}
    for s in snapshots: by_sector.setdefault(s.sector,[]).append(s)
    stats={}
    for sector,snaps in by_sector.items():
        pe=[s.pe_ratio for s in snaps if s.pe_ratio and 0<s.pe_ratio<200]
        pb=[s.pb_ratio for s in snaps if s.pb_ratio and 0<s.pb_ratio<50]
        ev=[s.ev_ebitda for s in snaps if s.ev_ebitda and 0<s.ev_ebitda<100]
        roe=[s.roe_pct for s in snaps if s.roe_pct and 0<s.roe_pct<80]
        fcf_y=[]
        for s in snaps:
            if s.market_cap and s.market_cap>0 and s.free_cash_flow and s.free_cash_flow>0:
                fcf_y.append(s.free_cash_flow/s.market_cap*100)
        stats[sector]=SectorStats(sector=sector,median_pe=_median(pe),median_pb=_median(pb),
            median_ev_ebitda=_median(ev),median_fcf_yield=_median(fcf_y),
            median_roe=_median(roe),stock_count=len(snaps))
    return stats

def inject_sector_stats(snapshot:MarketSnapshot,stats:dict)->MarketSnapshot:
    s=stats.get(snapshot.sector)
    if s is None: return snapshot
    snapshot.sector_median_pe=s.median_pe; snapshot.sector_median_pb=s.median_pb
    snapshot.sector_median_ev_ebitda=s.median_ev_ebitda
    snapshot.sector_median_fcf_yield=s.median_fcf_yield
    return snapshot

def valuation_verdict(score:float)->str:
    if score>=65: return UNDERVALUED
    if score>=40: return FAIRLY_VALUED
    return OVERVALUED

def score_valuation_pillar(snapshot:MarketSnapshot)->tuple:
    facts=[]; subs=[]
    if snapshot.pe_ratio and snapshot.sector_median_pe:
        r=snapshot.pe_ratio/snapshot.sector_median_pe
        subs.append(_scale(-r,-3.0,-0.3))
        disc=(1-r)*100
        facts.append(f"PE {snapshot.pe_ratio:.1f}x vs sector median {snapshot.sector_median_pe:.1f}x "
                     f"({'discount' if disc>0 else 'premium'} {abs(disc):.0f}%)")
    elif snapshot.pe_ratio and snapshot.sector_avg_pe:
        subs.append(_scale(-(snapshot.pe_ratio/snapshot.sector_avg_pe),-3.0,-0.3))
        facts.append(f"PE {snapshot.pe_ratio:.1f}x vs sector avg {snapshot.sector_avg_pe:.1f}x")
    if snapshot.pb_ratio and snapshot.sector_median_pb:
        subs.append(_scale(-(snapshot.pb_ratio/snapshot.sector_median_pb),-4.0,-0.4))
        facts.append(f"PB {snapshot.pb_ratio:.1f}x vs sector median {snapshot.sector_median_pb:.1f}x")
    if snapshot.ev_ebitda and snapshot.sector_median_ev_ebitda:
        subs.append(_scale(-(snapshot.ev_ebitda/snapshot.sector_median_ev_ebitda),-3.0,-0.4))
        facts.append(f"EV/EBITDA {snapshot.ev_ebitda:.1f}x vs sector median {snapshot.sector_median_ev_ebitda:.1f}x")
    if snapshot.analyst_target_mean and snapshot.price and snapshot.price>0:
        upside=(snapshot.analyst_target_mean-snapshot.price)/snapshot.price*100
        subs.append(_scale(upside,-15,35))
        n=f", {snapshot.analyst_count} analysts" if snapshot.analyst_count else ""
        facts.append(f"analyst target ₹{snapshot.analyst_target_mean:.0f} ({upside:+.1f}%{n})")
    if not subs: subs.append(50.0); facts.append("valuation data unavailable")
    score=round(sum(subs)/len(subs),1)
    return _clip(score), valuation_verdict(score), facts
