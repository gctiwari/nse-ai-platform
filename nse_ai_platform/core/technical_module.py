"""§5 Technical Analysis module — 8 indicators, composite Buy/Neutral/Sell."""
from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np
import pandas as pd

BUY="Buy"; NEUTRAL="Neutral"; SELL="Sell"

@dataclass
class IndicatorSignal:
    name:str; value:str; signal:str; reason:str

@dataclass
class TechnicalAnalysisResult:
    symbol:str; composite_signal:str; confidence:float
    bullish_count:int; bearish_count:int; neutral_count:int
    indicators:list=field(default_factory=list); summary:str=""

def _ema(s,n): return s.ewm(span=n,adjust=False).mean()
def _rsi(c,p=14):
    d=c.diff(); g=d.clip(lower=0).rolling(p).mean().iloc[-1]; l=(-d.clip(upper=0)).rolling(p).mean().iloc[-1]
    return round(100.0 if l==0 else 100-100/(1+g/l),1)
def _macd(c):
    ml=_ema(c,12)-_ema(c,26); sl=_ema(ml,9); h=ml-sl
    return round(float(ml.iloc[-1]),3),round(float(sl.iloc[-1]),3),round(float(h.iloc[-1]),3)
def _bb(c,w=20,ns=2.0):
    mid=c.rolling(w).mean(); std=c.rolling(w).std()
    return round(float((mid+ns*std).iloc[-1]),2),round(float(mid.iloc[-1]),2),round(float((mid-ns*std).iloc[-1]),2)
def _stoch(hi,lo,cl,kp=14,dp=3):
    ll=lo.rolling(kp).min(); hh=hi.rolling(kp).max(); den=(hh-ll).replace(0,np.nan)
    k=((cl-ll)/den*100).rolling(dp).mean(); d=k.rolling(dp).mean()
    return round(float(k.iloc[-1]),1),round(float(d.iloc[-1]),1)
def _wr(hi,lo,cl,p=14):
    hh=hi.rolling(p).max().iloc[-1]; ll=lo.rolling(p).min().iloc[-1]
    return round((hh-cl.iloc[-1])/(hh-ll)*-100 if hh!=ll else -50.0,1)
def _adx(hi,lo,cl,p=14):
    um=hi.diff(); dm=-lo.diff()
    pdm=np.where((um>dm)&(um>0),um,0.0); mdm=np.where((dm>um)&(dm>0),dm,0.0)
    tr=pd.concat([hi-lo,(hi-cl.shift()).abs(),(lo-cl.shift()).abs()],axis=1).max(axis=1)
    atr=tr.rolling(p).mean()
    pdi=pd.Series(pdm,index=hi.index).rolling(p).mean()/atr.replace(0,np.nan)*100
    mdi=pd.Series(mdm,index=hi.index).rolling(p).mean()/atr.replace(0,np.nan)*100
    dx=(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan)*100
    return round(float(dx.rolling(p).mean().iloc[-1]),1)
def _obv_dir(cl,vol,lb=20):
    d=cl.diff().apply(lambda x:1 if x>0 else(-1 if x<0 else 0))
    obv=(d*vol).cumsum(); r=obv.tail(lb)
    s=(r.iloc[-1]-r.iloc[0])/max(abs(r.iloc[0]),1)
    return "Rising" if s>0.01 else "Falling" if s<-0.01 else "Flat"

def _pad(s,target):
    if len(s)<target:
        p=pd.Series([s.iloc[0]]*(target-len(s))); return pd.concat([p,s],ignore_index=True)
    return s

def analyse(symbol,price,price_history,high_history,low_history,volume_history,week52_high,week52_low):
    MB=30
    cl=_pad(pd.Series(price_history,dtype=float),MB)
    hi=_pad(pd.Series(high_history,dtype=float) if high_history and len(high_history)==len(price_history) else cl*1.008,MB)
    lo=_pad(pd.Series(low_history,dtype=float)  if low_history  and len(low_history) ==len(price_history) else cl*0.992,MB)
    vol=_pad(pd.Series(volume_history,dtype=float) if volume_history and len(volume_history)==len(price_history) else pd.Series([1e6]*len(pd.Series(price_history))),MB)
    sigs=[]
    # RSI
    rv=_rsi(cl)
    if rv<30: rs,rr=BUY,f"oversold ({rv})"
    elif rv>70: rs,rr=SELL,f"overbought ({rv})"
    elif 40<=rv<=60: rs,rr=NEUTRAL,f"neutral ({rv})"
    elif rv<40: rs,rr=NEUTRAL,f"weak ({rv})"
    else: rs,rr=BUY,f"mildly bullish ({rv})"
    sigs.append(IndicatorSignal("RSI (14)",str(rv),rs,rr))
    # MACD
    mv,msv,mh=_macd(cl)
    if mv>msv and mh>0: ms,mr=BUY,f"above signal, +histogram"
    elif mv<msv and mh<0: ms,mr=SELL,f"below signal, -histogram"
    else: ms,mr=NEUTRAL,f"crossing ({mv:.2f}/{msv:.2f})"
    sigs.append(IndicatorSignal("MACD",f"{mv:.2f}/{msv:.2f}",ms,mr))
    # Bollinger
    bu,bm,bl=_bb(cl)
    if price<bl: bs,br=BUY,f"below lower band ₹{bl:.1f}"
    elif price>bu: bs,br=SELL,f"above upper band ₹{bu:.1f}"
    else: bs,br=NEUTRAL,f"within bands (mid ₹{bm:.1f})"
    sigs.append(IndicatorSignal("Bollinger (20,2σ)",f"U:{bu:.1f}/M:{bm:.1f}/L:{bl:.1f}",bs,br))
    # Stochastic
    sk,sd=_stoch(hi,lo,cl)
    if sk<20 and sd<20: ss,sr=BUY,f"oversold %K{sk}/%D{sd}"
    elif sk>80 and sd>80: ss,sr=SELL,f"overbought %K{sk}/%D{sd}"
    elif sk>sd: ss,sr=BUY,f"%K{sk} crossing above %D{sd}"
    else: ss,sr=NEUTRAL,f"%K{sk}/%D{sd}"
    sigs.append(IndicatorSignal("Stochastic (14-3-3)",f"%K {sk}/%D {sd}",ss,sr))
    # Williams %R
    wr=_wr(hi,lo,cl)
    if wr<-80: ws,wr2=BUY,f"oversold ({wr})"
    elif wr>-20: ws,wr2=SELL,f"overbought ({wr})"
    else: ws,wr2=NEUTRAL,f"mid-range ({wr})"
    sigs.append(IndicatorSignal("Williams %R (14)",str(wr),ws,wr2))
    # ADX
    av=_adx(hi,lo,cl)
    if av>=25: as_,ar=BUY,f"strong trend ({av:.0f})"
    else: as_,ar=NEUTRAL,f"weak/no trend ({av:.0f})"
    sigs.append(IndicatorSignal("ADX (14)",f"{av:.0f}",as_,ar))
    # Volume
    vn=float(vol.iloc[-1]); va=float(vol.tail(20).mean()); vr=vn/va if va>0 else 1.0
    od=_obv_dir(cl,vol)
    if vr>1.5 and od=="Rising": vs,vrs=BUY,f"{vr:.1f}× avg, OBV rising"
    elif vr>1.5 and od=="Falling": vs,vrs=SELL,f"{vr:.1f}× avg, OBV falling"
    else: vs,vrs=NEUTRAL,f"{vr:.1f}× avg, OBV {od.lower()}"
    sigs.append(IndicatorSignal("Volume",f"{vr:.1f}× avg",vs,vrs))
    # 52-week
    if week52_high>0:
        pfh=(price-week52_high)/week52_high*100
        pfl=(price-week52_low)/week52_low*100 if week52_low>0 else 0
        if pfh>=-5: rng_s,rng_r=SELL,f"within 5% of 52wk high"
        elif pfl<=10: rng_s,rng_r=BUY,f"within 10% of 52wk low"
        else: rng_s,rng_r=NEUTRAL,f"{abs(pfh):.0f}% below 52wk high"
        sigs.append(IndicatorSignal("52-Week Range",f"H:{week52_high:.0f}/L:{week52_low:.0f}",rng_s,rng_r))
    votes={BUY:0,NEUTRAL:0,SELL:0}
    for s in sigs: votes[s.signal]+=1
    tot=sum(votes.values()); bull=votes[BUY]/tot if tot else 0.5; bear=votes[SELL]/tot if tot else 0.5
    comp=BUY if bull>=0.60 else SELL if bear>=0.60 else NEUTRAL
    net=votes[BUY]-votes[SELL]; conf=round(abs(net)/tot*100,1) if tot else 0.0
    summary=f"{votes[BUY]} bullish, {votes[NEUTRAL]} neutral, {votes[SELL]} bearish — {comp} ({conf:.0f}% confidence)"
    return TechnicalAnalysisResult(symbol=symbol,composite_signal=comp,confidence=conf,
        bullish_count=votes[BUY],bearish_count=votes[SELL],neutral_count=votes[NEUTRAL],
        indicators=sigs,summary=summary)
