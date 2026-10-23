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


from readiness import features as base_features, points_vec  # noqa: E402  (run as: python scanner/precursors.py)

COST = float(os.getenv("SIM_COST", "0.02"))      # round-trip costs (spread + slippage), small caps
TARGET, STOP, GAP_MAX, HOLD = 0.30, 0.15, 0.15, 5


def features(df, rsplits):
    out = base_features(df, rsplits)
    c, h, l, o = df.c, df.h, df.l, df.o
    med20 = df.v.shift(1).rolling(20, min_periods=15).median()
    fwd_max = pd.concat([c.shift(-k) for k in range(1, 6)], axis=1).max(axis=1)
    out["run5"] = (fwd_max / c - 1 >= 0.30)
    out["pop1"] = (h.shift(-1) / c - 1 >= 0.30)
    out["fwd5"] = c.shift(-5) / c - 1
    out["valid"] = fwd_max.notna() & med20.notna()
    # --- trade simulation: buy next open (skip if it gaps >=15%, like the scanner), hold up to 5 days
    entry = o.shift(-1)
    out["gap_next"] = entry / c - 1
    H = np.column_stack([h.shift(-k).values for k in range(1, HOLD + 1)])
    L = np.column_stack([l.shift(-k).values for k in range(1, HOLD + 1)])
    last_close = c.shift(-HOLD).values
    e = entry.values
    tgt, stp = e * (1 + TARGET), e * (1 - STOP)
    hit_t = H >= tgt[:, None]
    hit_s = L <= stp[:, None]
    first_t = np.where(hit_t.any(1), hit_t.argmax(1), 99)
    first_s = np.where(hit_s.any(1), hit_s.argmax(1), 99)
    # A: target or day-5 close (no stop)
    exit_a = np.where(first_t < 99, tgt, last_close)
    # B: with -15% stop; if stop and target on the same day assume the stop came first (conservative)
    exit_b = np.where((first_s < 99) & (first_s <= first_t), stp, np.where(first_t < 99, tgt, last_close))
    with np.errstate(invalid="ignore", divide="ignore"):
        out["trade_a"] = exit_a / e - 1 - COST
        out["trade_b"] = exit_b / e - 1 - COST
    out["hit_target"] = first_t < 99
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
    # ---- tradability
    tr = panel[(panel.gap_next < GAP_MAX) & panel.trade_a.notna()]
    lines += ["## האם אפשר להרוויח מזה? סימולציית מסחר", "",
              f"כניסה בפתיחה של למחרת (מדלגים אם הפער בפתיחה {GAP_MAX*100:.0f}%+, כמו הסורק). יציאה ב-+{TARGET*100:.0f}% או בסגירה של יום {HOLD}. "
              f"עלויות {COST*100:.0f}% לעסקה. **A** = בלי סטופ. **B** = עם סטופ של ‎-{STOP*100:.0f}% (אם סטופ ויעד באותו יום, מניחים שהסטופ קודם).", "",
              "| כלל | עסקאות | הגיעו ליעד | A ממוצע | A חציון | A הצלחה | B ממוצע | B הצלחה | ‎-20% (A) |",
              "|---|---|---|---|---|---|---|---|---|"]
    def trow(name, d):
        if len(d) < 200:
            return None
        return (f"| {name} | {len(d):,} | {d.hit_target.mean()*100:.1f}% | {d.trade_a.mean()*100:+.2f}% | {d.trade_a.median()*100:+.1f}% | "
                f"{(d.trade_a > 0).mean()*100:.0f}% | {d.trade_b.mean()*100:+.2f}% | {(d.trade_b > 0).mean()*100:.0f}% | {(d.trade_a <= -0.2).mean()*100:.1f}% |",
                float(d.trade_a.mean()), float(d.trade_b.mean()), len(d))
    rows_t = []
    r0 = trow("כל הימים (בסיס)", tr)
    if r0:
        lines.append(r0[0])
    for name, fn in RULES.items():
        r = trow(name, tr[fn(tr).fillna(False).astype(bool)])
        if r:
            rows_t.append((name, r))
    for name, r in sorted(rows_t, key=lambda x: -x[1][1]):
        lines.append(r[0])
    res["trade_rules"] = {n: {"mean_a": r[1], "mean_b": r[2], "n": r[3]} for n, r in rows_t}
    lines += ["", "### צירופים (10 החזקים לפי רווח ממוצע A, לפחות 200 עסקאות)", "",
              "| צירוף | עסקאות | הגיעו ליעד | A ממוצע | A חציון | A הצלחה | B ממוצע | B הצלחה | ‎-20% (A) |",
              "|---|---|---|---|---|---|---|---|---|"]
    names = list(RULES)
    combo = []
    for i in range(len(names)):
        mi = RULES[names[i]](tr).fillna(False).astype(bool)
        for j in range(i + 1, len(names)):
            r = trow(f"{names[i]} + {names[j]}", tr[mi & RULES[names[j]](tr).fillna(False).astype(bool)])
            if r:
                combo.append(r)
    for r in sorted(combo, key=lambda x: -x[1])[:10]:
        lines.append(r[0])
    # ---- readiness score validation
    tr = tr.assign(ready=points_vec(tr))
    lines += ["", "## ציון המוכנות של הסורק: האם ציון גבוה = יותר ריצות?", "",
              "| ציון | ימים | run5 | pop1 | A ממוצע | B ממוצע | ‎-20% (A) |", "|---|---|---|---|---|---|---|"]
    rs = {}
    for k in sorted(tr.ready.unique()):
        d = tr[tr.ready == k] if k < 6 else tr[tr.ready >= 6]
        if k > 6 or len(d) < 100:
            continue
        lab = f"{k}+" if k == 6 else str(k)
        lines.append(f"| {lab} | {len(d):,} | {d.run5.mean()*100:.2f}% | {d.pop1.mean()*100:.2f}% | {d.trade_a.mean()*100:+.2f}% | "
                     f"{d.trade_b.mean()*100:+.2f}% | {(d.trade_a <= -0.2).mean()*100:.1f}% |")
        rs[lab] = {"n": len(d), "run5": float(d.run5.mean()), "pop1": float(d.pop1.mean()), "mean_a": float(d.trade_a.mean())}
    res["readiness_buckets"] = rs
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
              "", "_הטיית שורדים: המדגם כולל רק מניות שנסחרות היום; מניות שנמחקו (רבות מהן אחרי איחוד/קריסה) חסרות, ולכן התוצאות אופטימיות._",
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
