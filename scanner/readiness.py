"""'Readiness' signals from the stock's own trading (daily bars), shared by the scanner and the research script.

Weights come from the event study in data/research (922 small caps, 2y, Oct 2026):
lift vs base rate of a +30% run within 5 days / +30% high next day.
"""
import numpy as np
import pandas as pd


def features(df, rsplits=()):
    """df: daily bars with columns o,h,l,c,v indexed by date. Indicators use data up to each row's close only."""
    c, h, l, v = df.c, df.h, df.l, df.v
    out = pd.DataFrame(index=df.index)
    ret1 = c.pct_change()
    med20 = v.shift(1).rolling(20, min_periods=15).median()
    out["price"] = c
    out["dollar_vol20"] = (c * v).rolling(20, min_periods=15).median()
    out["ret_1d"] = ret1
    out["ret_3d"] = c / c.shift(3) - 1
    out["ret_10d"] = c / c.shift(10) - 1
    out["rvol_1"] = v / med20
    out["rvol_3"] = v.rolling(3).mean() / med20.shift(2)
    out["vol_days_5"] = (v / med20 > 1.5).astype(int).rolling(5).sum()
    up = (ret1 > 0)
    upv = (v * up).rolling(10).sum(); dnv = (v * (~up)).rolling(10).sum()
    out["updown_vol_10"] = upv / dnv.replace(0, np.nan)
    rng = (h - l).replace(0, np.nan)
    clv = ((c - l) - (h - c)) / rng
    out["clv_1"] = clv
    out["clv_3"] = clv.rolling(3).mean()
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    out["atr_ratio"] = tr.rolling(5).mean() / tr.rolling(20).mean()
    sd = c.rolling(20).std(); ma = c.rolling(20).mean()
    out["bbw_pct"] = (4 * sd / ma).rolling(120, min_periods=60).rank(pct=True)
    out["near_20d_high"] = c / h.rolling(20).max()
    out["drawdown_60"] = c / h.rolling(60, min_periods=40).max() - 1
    obv = (np.sign(ret1).fillna(0) * v).cumsum()
    out["obv_div"] = ((obv - obv.shift(10)) / v.rolling(20).mean() > 2) & (out["ret_10d"] <= 0)
    out["vol20"] = ret1.rolling(20).std()
    big = (ret1 >= 0.30)
    out["prior_runs_120"] = big.shift(1).rolling(120, min_periods=20).sum()
    out["recent_run_10"] = big.rolling(10).sum()
    ds = pd.Series(np.nan, index=df.index)
    for d in rsplits:
        m = (df.index >= d)
        ds[m] = np.fmin(ds[m].fillna(9999), (df.index[m] - d).days)
    out["days_since_rsplit"] = ds
    return out


def points(f):
    """f: one row (Series/dict) of features. Returns (points, reasons, risk_flags)."""
    g = lambda k: (None if f.get(k) is None or (isinstance(f.get(k), float) and np.isnan(f.get(k))) else f.get(k))
    pts, why, risk = 0, [], []
    rv = g("rvol_1")
    if rv is not None and rv >= 5:
        pts += 2; why.append(f"נפח אתמול פי {rv:.1f}")
    elif rv is not None and rv >= 3:
        pts += 1; why.append(f"נפח אתמול פי {rv:.1f}")
    ar = g("atr_ratio")
    if rv is not None and rv >= 3 and ar is not None and ar <= 0.6:
        pts += 1; why.append("נפח חריג אחרי טווח מכווץ")
    dd = g("drawdown_60")
    if dd is not None and dd <= -0.5:
        pts += 1; why.append(f"ירדה {dd*100:.0f}% משיא 60 יום")
    vo = g("vol20")
    if vo is not None and vo >= 0.08:
        pts += 1; why.append(f"תנודתית ({vo*100:.0f}% ביום)")
    p = g("price")
    if p is not None and p < 1:
        pts += 1; why.append("מתחת ל-$1")
    pr = g("prior_runs_120")
    if pr is not None and pr >= 2:
        pts += 1; why.append(f"{int(pr)} ריצות ב-120 יום")
    r1 = g("ret_1d")
    if r1 is not None and r1 >= 0.10:
        pts += 1; why.append(f"עלתה {r1*100:.0f}% אתמול")
    rs = g("days_since_rsplit")
    if rs is not None and rs <= 60:
        pts += 1; why.append(f"איחוד מניות לפני {int(rs)} ימים")
        risk.append("איחוד מניות: גם סיכוי גבוה לנפילה")
    nh = g("near_20d_high")
    if nh is not None and nh >= 0.97:
        pts -= 1; why.append("ליד שיא 20 יום (פחות ריצות)")
    return pts, why, risk


def last_completed(df, today_et):
    """Drop a bar dated today (partial session), so signals describe 'yesterday'."""
    if len(df) and df.index[-1].date() >= today_et:
        return df.iloc[:-1]
    return df


def points_vec(fe):
    """Vectorized version of points() for the research panel (same thresholds)."""
    rv, ar = fe.rvol_1, fe.atr_ratio
    p = ((rv >= 5) * 2 + ((rv >= 3) & (rv < 5)) * 1 + ((rv >= 3) & (ar <= 0.6)) * 1
         + (fe.drawdown_60 <= -0.5) * 1 + (fe.vol20 >= 0.08) * 1 + (fe.price < 1) * 1
         + (fe.prior_runs_120 >= 2) * 1 + (fe.ret_1d >= 0.10) * 1 + (fe.days_since_rsplit <= 60) * 1
         - (fe.near_20d_high >= 0.97) * 1)
    return p.astype(int)
