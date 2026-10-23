"""Event study: which price/volume patterns in the days BEFORE a run are more common than usual?

For a sample of US-listed small/low-priced stocks it downloads ~1-2 years of daily bars (Yahoo),
builds a daily panel of indicators computed only from data up to that day's close, and measures
how often a run follows (label), compared with the base rate. Output: Hebrew markdown report.

Labels (forward-looking, from day t close):
  run5  = max close over t+1..t+5 >= +30%
  pop1  = high on t+1 >= +30%
Rows: only days without a >=30% up-day in the previous 10 days (looking for the FIRST leg).
"""
import json, os, random, sys, time, math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

N_TICKERS = int(os.getenv("N_TICKERS", "2500"))
RANGE = os.getenv("RANGE", "2y")
OUT_DIR = os.getenv("OUT_DIR", "data/research")
SEC_UA = os.getenv("SEC_USER_AGENT", "").strip() or "small-cap-buzz research github.com/tzachistocks"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
RNG = random.Random(42)
errors = []


def universe():
    r = requests.get("https://www.sec.gov/files/company_tickers_exchange.json", headers={"User-Agent": SEC_UA}, timeout=30)
    r.raise_for_status()
    d = r.json()
    f = d["fields"]
    rows = [dict(zip(f, x)) for x in d["data"]]
    ok = [x["ticker"] for x in rows if x.get("exchange") in ("Nasdaq", "NYSE", "NYSE American", "NYSE MKT", "NYSEAMERICAN")
          and x["ticker"].isalpha() and 1 <= len(x["ticker"]) <= 5]
    ok = sorted(set(ok))
    RNG.shuffle(ok)
    return ok


def bars(t):
    for host in ("query1", "query2"):
        for attempt in range(3):
            try:
                r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{t}",
                                 params={"range": RANGE, "interval": "1d", "events": "split"},
                                 headers={"User-Agent": UA}, timeout=20)
                if r.status_code == 429:
                    time.sleep(5 * (attempt + 1)); continue
                if r.status_code != 200:
                    break
                res = r.json()["chart"]["result"][0]
                q = res["indicators"]["quote"][0]
                df = pd.DataFrame({"o": q["open"], "h": q["high"], "l": q["low"], "c": q["close"], "v": q["volume"]},
                                  index=pd.to_datetime(res["timestamp"], unit="s").normalize())
                df = df.dropna()
                df = df[(df.c > 0) & (df.v >= 0)]
                splits = (res.get("events") or {}).get("splits") or {}
                rs = [pd.to_datetime(int(k), unit="s").normalize() for k, s in splits.items()
                      if s.get("numerator") and s.get("denominator") and s["numerator"] < s["denominator"]]
                return df[~df.index.duplicated()], rs
            except Exception as e:
                errors.append(f"{t}: {type(e).__name__}")
                time.sleep(1)
    return None, []


def features(df, rsplits):
    c, h, l, v, o = df.c, df.h, df.l, df.v, df.o
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
    bbw = (4 * sd / ma)
    out["bbw_pct"] = bbw.rolling(120, min_periods=60).rank(pct=True)
    out["near_20d_high"] = c / h.rolling(20).max()
    out["drawdown_60"] = c / h.rolling(60, min_periods=40).max() - 1
    obv = (np.sign(ret1).fillna(0) * v).cumsum()
    out["obv_div"] = ((obv - obv.shift(10)) / v.rolling(20).mean() > 2) & (out["ret_10d"] <= 0)
    out["vol20"] = ret1.rolling(20).std()
    big = (ret1 >= 0.30)
    out["prior_runs_120"] = big.shift(1).rolling(120, min_periods=20).sum()
    out["recent_run_10"] = big.rolling(10).sum()
    # days since reverse split
    ds = pd.Series(np.nan, index=df.index)
    for d in rsplits:
        m = (df.index >= d)
        ds[m] = np.fmin(ds[m].fillna(9999), (df.index[m] - d).days)
    out["days_since_rsplit"] = ds
    # labels
    fwd_max = pd.concat([c.shift(-k) for k in range(1, 6)], axis=1).max(axis=1)
    out["run5"] = (fwd_max / c - 1 >= 0.30)
    out["pop1"] = (h.shift(-1) / c - 1 >= 0.30)
    out["fwd5"] = c.shift(-5) / c - 1
    out["valid"] = fwd_max.notna() & med20.notna()
    return out


RULES = {
    "נפח אתמול פי 3+ (rvol_1≥3)": lambda d: d.rvol_1 >= 3,
    "נפח אתמול פי 5+": lambda d: d.rvol_1 >= 5,
    "נפח 3 ימים פי 2+ בלי תזוזת מחיר (איסוף שקט)": lambda d: (d.rvol_3 >= 2) & (d.ret_3d.abs() < 0.10),
    "3+ ימי נפח חריג מתוך 5": lambda d: d.vol_days_5 >= 3,
    "נפח בימי עלייה פי 2 מימי ירידה (10 ימים)": lambda d: d.updown_vol_10 >= 2,
    "סגירה ליד שיא היום (CLV≥0.6)": lambda d: d.clv_1 >= 0.6,
    "3 סגירות ליד השיא (CLV ממוצע≥0.5)": lambda d: d.clv_3 >= 0.5,
    "התכווצות טווח (ATR5/ATR20≤0.6)": lambda d: d.atr_ratio <= 0.6,
    "בולינגר צר (10% התחתונים)": lambda d: d.bbw_pct <= 0.10,
    "ליד שיא 20 יום (≥97%)": lambda d: d.near_20d_high >= 0.97,
    "ירידה של 50%+ משיא 60 יום": lambda d: d.drawdown_60 <= -0.50,
    "OBV עולה כשהמחיר לא עולה": lambda d: d.obv_div.astype(bool),
    "מחיר מתחת ל-$1": lambda d: d.price < 1,
    "מחיר $1-3": lambda d: (d.price >= 1) & (d.price < 3),
    "מחיר $3-10": lambda d: (d.price >= 3) & (d.price < 10),
    "2+ ריצות של 30% ב-120 יום": lambda d: d.prior_runs_120 >= 2,
    "איחוד מניות ב-60 יום אחרונים": lambda d: d.days_since_rsplit <= 60,
    "עלייה של 10%+ אתמול": lambda d: d.ret_1d >= 0.10,
    "תנודתיות גבוהה (סטיית תקן יומית ≥8%)": lambda d: d.vol20 >= 0.08,
}
DECILE_FEATS = ["rvol_1", "rvol_3", "updown_vol_10", "clv_3", "atr_ratio", "bbw_pct", "near_20d_high",
                "drawdown_60", "ret_10d", "vol20", "dollar_vol20", "price"]


def stats(mask, d, label):
    m = mask.fillna(False).astype(bool)
    n = int(m.sum())
    if n == 0:
        return None
    sub = d[m]
    return {"n": n, "hit": float(sub[label].mean()), "fwd5_med": float(sub.fwd5.median()),
            "down20": float((sub.fwd5 <= -0.20).mean())}


def main():
    t0 = time.time()
    os.makedirs(OUT_DIR, exist_ok=True)
    tick = universe()
    print(f"universe {len(tick)}; sampling {N_TICKERS}", flush=True)
    tick = tick[:N_TICKERS]
    frames = []
    done = 0
    with ThreadPoolExecutor(6) as ex:
        futs = {ex.submit(bars, t): t for t in tick}
        for f in as_completed(futs):
            t = futs[f]; done += 1
            df, rs = f.result()
            if done % 250 == 0:
                print(f"{done}/{len(tick)} ({time.time()-t0:.0f}s)", flush=True)
            if df is None or len(df) < 120:
                continue
            # small / low-priced, tradable
            if df.c.tail(120).median() > 20 or (df.c * df.v).tail(120).median() < 100_000:
                continue
            fe = features(df, rs)
            fe["ticker"] = t
            frames.append(fe)
    panel = pd.concat(frames)
    panel = panel[panel.valid & (panel.recent_run_10 == 0) & (panel.price >= 0.10)]
    print(f"panel rows {len(panel):,} tickers {panel.ticker.nunique()}", flush=True)

    res = {"generated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "tickers": int(panel.ticker.nunique()),
           "rows": int(len(panel)), "range": RANGE}
    lines = [f"# מה קודם להרצות? ניתוח {res['tickers']} מניות קטנות, {res['rows']:,} ימי מסחר ({RANGE})", "",
             "**שורה = יום מסחר.** האינדיקטורים מחושבים עד הסגירה של אותו יום בלבד. נבדקים רק ימים בלי ריצה של 30%+ ב-10 הימים שלפני (חיפוש הרגל הראשונה).",
             "- **run5** = סגירה כלשהי ב-5 הימים הבאים גבוהה ב-30%+ מהסגירה של היום.",
             "- **pop1** = השיא של מחר גבוה ב-30%+ מהסגירה של היום.",
             "- **lift** = כמה פעמים יותר מהשכיחות הרגילה. **fwd5** = חציון התשואה אחרי 5 ימים. **‎-20%** = שיעור הימים שבהם אחרי 5 ימים המניה ירדה 20%+.", ""]
    for label in ("run5", "pop1"):
        base = float(panel[label].mean())
        res[f"base_{label}"] = base
        lines += [f"## {label}: שכיחות בסיס {base*100:.2f}%", "",
                  "| כלל | ימים | פגיעה | lift | fwd5 חציון | ‎-20% ב-5 ימים |", "|---|---|---|---|---|---|"]
        rule_rows = []
        for name, fn in RULES.items():
            s = stats(fn(panel), panel, label)
            if s and s["n"] >= 300:
                s["lift"] = s["hit"] / base if base else 0
                rule_rows.append((name, s))
        for name, s in sorted(rule_rows, key=lambda x: -x[1]["lift"]):
            lines.append(f"| {name} | {s['n']:,} | {s['hit']*100:.2f}% | {s['lift']:.2f} | {s['fwd5_med']*100:+.1f}% | {s['down20']*100:.1f}% |")
        res[f"rules_{label}"] = {n: s for n, s in rule_rows}
        # pairs
        names = [n for n, _ in rule_rows]
        pairs = []
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                m = RULES[names[i]](panel).fillna(False).astype(bool) & RULES[names[j]](panel).fillna(False).astype(bool)
                s = stats(m, panel, label)
                if s and s["n"] >= 200:
                    s["lift"] = s["hit"] / base
                    pairs.append((f"{names[i]} + {names[j]}", s))
        pairs.sort(key=lambda x: -x[1]["lift"])
        lines += ["", f"### 15 הצירופים החזקים ({label}, לפחות 200 ימים)", "",
                  "| צירוף | ימים | פגיעה | lift | fwd5 חציון | ‎-20% |", "|---|---|---|---|---|---|"]
        for name, s in pairs[:15]:
            lines.append(f"| {name} | {s['n']:,} | {s['hit']*100:.2f}% | {s['lift']:.2f} | {s['fwd5_med']*100:+.1f}% | {s['down20']*100:.1f}% |")
        res[f"pairs_{label}"] = dict(pairs[:40])
        lines.append("")
    # deciles for run5
    lines += ["## עשירונים (run5): שיעור הפגיעה לפי עשירון של כל אינדיקטור", "",
              "| אינדיקטור | " + " | ".join(f"D{i}" for i in range(1, 11)) + " |", "|---|" + "---|" * 10]
    dec = {}
    for ftr in DECILE_FEATS:
        x = panel[[ftr, "run5"]].dropna()
        if len(x) < 5000:
            continue
        q = pd.qcut(x[ftr].rank(method="first"), 10, labels=False)
        rates = x.groupby(q)["run5"].mean().tolist()
        dec[ftr] = rates
        lines.append(f"| {ftr} | " + " | ".join(f"{r*100:.1f}" for r in rates) + " |")
    res["deciles_run5"] = dec
    lines += ["", f"_זמן ריצה {time.time()-t0:.0f} שניות; שגיאות הורדה: {len(errors)}_",
              "", "מידע מחקרי, לא המלצת השקעה. תוצאות עבר על מדגם אינן מבטיחות תוצאות עתידיות."]
    md = "\n".join(lines)
    with open(os.path.join(OUT_DIR, "precursors_report.md"), "w") as f:
        f.write(md)
    with open(os.path.join(OUT_DIR, "precursors_results.json"), "w") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=float)
    p = os.getenv("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a") as f:
            f.write(md + "\n")
    print(md)


if __name__ == "__main__":
    main()
