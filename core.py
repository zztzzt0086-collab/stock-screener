"""
core.py — 데이터 수집 + 채점 엔진 (UI 없음)
==========================================
app.py(웹/폰)와 cli.py(명령어창)가 이 파일 하나를 같이 씀.
로직 수정은 여기서만 하면 양쪽에 동시 반영된다.

streamlit이 설치돼 있으면 st.cache_data를, 없으면(CLI 단독 실행 시)
파일 캐시로 자동 대체된다.
"""

import json
import os
import time
from datetime import datetime, timedelta

import logging

import pandas as pd
import yfinance as yf

# yfinance 가 상장폐지/티커오류 종목마다 HTTP 404 를 화면에 찍어서 시끄럽다.
# 어차피 해당 종목은 건너뛰므로 로그만 조용히 시킨다.
for _n in ("yfinance", "yfinance.data", "yfinance.utils", "peewee", "urllib3"):
    logging.getLogger(_n).setLevel(logging.CRITICAL)

# ═════════════════════════════════════════════════════════════
# 채점 로직 버전
#   점수 매기는 기준을 고칠 때마다 이 숫자를 올린다.
#   history/ 에 함께 저장되므로, 나중에 verify 를 돌릴 때
#   "같은 잣대로 매긴 점수끼리 비교하는지" 확인할 수 있다.
#
#   1  2026-09-12  최초. 성장 170점 / 장기 120점 / 수급 가중평균
#                  (배당·부채비율·주식수·이자보상 계산 오류 수정본)
# ═════════════════════════════════════════════════════════════
SCORE_VERSION = 1

USD_KRW = 1380

# 색상 (app.py 와 공용)
CHARCOAL = "#2F3437"
ORANGE = "#EA580C"
AMBER = "#B45309"
BLUE = "#2563EB"
GRAY = "#9CA3AF"
억 = 1_0000_0000
조 = 1_0000_0000_0000

CACHE_DIR = ".cache"


# ─────────────────────────────────────────────────────────────
# 캐시: streamlit 있으면 st.cache_data, 없으면 파일 캐시
# ─────────────────────────────────────────────────────────────

def _file_cache(ttl):
    """CLI 전용 디스크 캐시 데코레이터."""
    def deco(fn):
        def wrap(*a):
            os.makedirs(CACHE_DIR, exist_ok=True)
            key = f"{fn.__name__}_{'_'.join(map(str, a))}".replace("/", "_")
            p = os.path.join(CACHE_DIR, key + ".json")
            if os.path.exists(p) and time.time() - os.path.getmtime(p) < ttl:
                try:
                    with open(p, encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    pass
            r = fn(*a)
            try:
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(r, f, ensure_ascii=False, default=str)
            except Exception:
                pass
            return r
        wrap.__name__ = fn.__name__
        return wrap
    return deco


def _in_streamlit():
    """streamlit 서버가 실제로 돌고 있는지. 단순 import 여부로는 부족하다."""
    try:
        from streamlit.runtime import exists
        return exists()
    except Exception:
        try:
            from streamlit.runtime.scriptrunner import get_script_run_ctx
            return get_script_run_ctx() is not None
        except Exception:
            return False


if _in_streamlit():
    import streamlit as st

    def cache(ttl):
        return st.cache_data(ttl=ttl, show_spinner=False)
else:                            # CLI 단독 실행 → 파일 캐시
    cache = _file_cache


# ═════════════════════════════════════════════════════════════
# 채점 로직
# ═════════════════════════════════════════════════════════════

def _row(df, keys):
    if df is None or df.empty:
        return None
    for k in keys:
        if k in df.index:
            s = df.loc[k].dropna()
            if len(s):
                return s.sort_index()
    return None


TEN_MAX = {"시가총액": 15, "영업이익률": 10, "부채비율": 10, "내부자지분": 10,
           "ROE": 5, "밸류에이션": 10, "성장 가속도": 20, "이익률 추세": 15,
           "어닝 서프라이즈": 15, "실적-주가 괴리": 10, "순현금/시총": 10,
           "주식수 희석": 10, "이익의 질": 10, "잉여현금흐름": 10,
           "R&D 집중도": 5, "하락 회복력": 5}

LT_MAX = {"FCF 안정성": 20, "이익률 안정성": 15, "자본수익률": 15,
          "침체 생존력": 15, "장기 성장률": 15, "주식수 관리": 10,
          "배당": 10, "부채 안전성": 10, "밸류에이션": 10}


def score_ten(d):
    o = []
    m = d["mcap_krw"]
    if m:
        o.append(("시가총액", 15 if m < 1000*억 else 13 if m < 5000*억
                  else 8 if m < 2*조 else 4 if m < 10*조 else 0,
                  "1천억↓" if m < 1000*억 else "1천~5천억" if m < 5000*억
                  else "5천억~2조" if m < 2*조 else "2~10조" if m < 10*조 else "10조↑"))
    v = d["margin"]
    if v is not None:
        o.append(("영업이익률", 10 if v >= 25 else 8 if v >= 15 else 4 if v >= 5
                  else 2 if v >= 0 else 0, f"{v:.1f}%"))
    v = d["debt"]
    if v is not None:
        asof = f" ({d['debt_asof']})" if d.get("debt_asof") else ""
        o.append(("부채비율", 10 if v < 50 else 8 if v < 100 else 4 if v < 200 else 0,
                  f"{v:.0f}%{asof}"))
    v = d["insider"]
    if v is not None:
        o.append(("내부자지분", 10 if v >= 30 else 8 if v >= 15 else 4 if v >= 5 else 1,
                  f"{v:.1f}%"))
    v = d["roe"]
    if v is not None:
        o.append(("ROE", 5 if v >= 20 else 4 if v >= 12 else 2 if v >= 5 else 0,
                  f"{v:.1f}%"))
    peg, per, g = d["peg"], d["per"], d["growth"]
    if peg and peg > 0:
        o.append(("밸류에이션", 10 if peg < 1 else 8 if peg < 1.5 else 5 if peg < 2.5
                  else 2 if peg < 4 else 0, f"PEG {peg:.2f}"))
    elif per and per > 0 and g and g > 0:
        r = per / g
        o.append(("밸류에이션", 10 if r < 1 else 8 if r < 1.5 else 5 if r < 2.5 else 2,
                  f"PER {per:.0f} (성장 {g:.0f}%)"))
    elif per and per > 0:
        # 성장률을 못 구한 경우(역성장 포함) PER 절대수준으로만 평가.
        # 성장 대비 평가가 아니므로 만점은 주지 않는다.
        o.append(("밸류에이션", 9 if per < 10 else 7 if per < 15 else 5 if per < 25
                  else 3 if per < 40 else 1 if per < 60 else 0,
                  f"PER {per:.1f} (성장률 미확인)"))
    seq = d.get("qgrowth")
    if seq and len(seq) >= 3:
        a_ = sum(seq[:2]) / 2
        b_ = sum(seq[2:4]) / len(seq[2:4]) if len(seq) >= 4 else seq[2]
        dl = a_ - b_
        tr = " → ".join(f"{x:.0f}" for x in reversed(seq[:4]))
        o.append(("성장 가속도",
                  20 if dl >= 15 else 16 if dl >= 5 else 10 if dl >= -5
                  else 4 if dl >= -15 else 0,
                  f"[{tr}%] " + ("강한 가속" if dl >= 15 else "가속" if dl >= 5
                                 else "유지" if dl >= -5 else "감속" if dl >= -15
                                 else "급감속")))
    q = d["qmargin"]
    if q and len(q) >= 3:
        a = sum(q[:2]) / 2
        b = sum(q[2:4]) / len(q[2:4]) if len(q) >= 4 else q[2]
        dd = a - b
        t = " → ".join(f"{x:.0f}" for x in reversed(q[:4]))
        o.append(("이익률 추세", 15 if dd >= 5 else 12 if dd >= 1.5 else 7 if dd >= -1.5
                  else 3 if dd >= -5 else 0, f"[{t}%]"))
    bt, tt = d["beats"]
    if tt:
        r = bt / tt
        o.append(("어닝 서프라이즈", 15 if r >= 1 else 12 if r >= .75 else 7 if r >= .5
                  else 3 if r >= .25 else 0, f"{bt}/{tt}분기"))
    rg, pg = d["rev1y"], d["px1y"]
    if rg is not None and pg is not None:
        gap = rg - pg
        o.append(("실적-주가 괴리", 10 if gap >= 50 else 8 if gap >= 20 else 5 if gap >= -20
                  else 2 if gap >= -50 else 0, f"매출{rg:+.0f}% 주가{pg:+.0f}%"))
    # 순현금은 재무제표 통화이므로 같은 통화의 시총과 비교해야 한다
    nc, mc = d["netcash"], d.get("mcap_fin")
    if nc is not None and mc:
        r = nc / mc * 100
        o.append(("순현금/시총", 10 if r >= 30 else 8 if r >= 10 else 5 if r >= 0
                  else 2 if r >= -20 else 0, f"{r:+.0f}%"))
    v = d["dilution"]
    if v is not None:
        base = (10 if v <= -2 else 8 if v <= 1 else 5 if v <= 4
                else 2 if v <= 10 else 0)
        note, dc, bv = "", d.get("debt_chg"), d.get("bvps_chg")
        de_now = d.get("debt")
        # 부채비율 자체가 낮으면(30% 미만) 증가율이 커도 위험 신호가 아니다.
        if de_now is not None and de_now < 30:
            dc = None
        if v <= -3 and dc is not None and dc >= 50:
            # 주식수는 줄었는데 빚이 크게 늘었다 = 차입 자사주매입.
            # 주주환원처럼 보이지만 재무 위험을 키운 것이라 만점을 줄 수 없다.
            base = min(base, 5)
            note = f" · 부채 {dc:+.0f}% 동반, 차입 소각 의심"
        elif v <= -10:
            note = " · 대규모 소각, 재원 확인 필요"
        elif v >= 4 and bv is not None:
            # 증자를 했어도 주당 장부가가 올랐다면 장부가보다 비싸게 발행한 것.
            # 반대로 주당 장부가가 줄었다면 실제로 주주가치가 깎인 것이다.
            if bv >= 5:
                base = max(base, 5)
                note = f" · 증자, 주당장부가 {bv:+.0f}% (희석 아님)"
            elif bv >= -2:
                base = max(base, 3)
                note = f" · 증자, 주당장부가 {bv:+.0f}% (보합)"
            else:
                base = min(base, 2)
                note = f" · 증자, 주당장부가 {bv:+.0f}% · 실질 희석"
        elif v >= 10:
            note = " · 대규모 증자"
        o.append(("주식수 희석", base, f"{v:+.1f}%{note}"))
    ocf, ni = d["ocf"], d["ni"]
    if ocf is not None and ni:
        if ni < 0:
            o.append(("이익의 질", 4 if ocf > 0 else 0,
                      "순손실, 영업현금 흑자" if ocf > 0 else "순손실, 영업현금 적자"))
        else:
            r = ocf / ni
            o.append(("이익의 질", 10 if r >= 1.3 else 8 if r >= 1 else 5 if r >= .7
                      else 2 if r >= .3 else 0, f"OCF/NI {r:.2f}"))
    fl, ol, cl = d.get("fcf_last"), d.get("ocf_last"), d.get("capex_last")
    if fl is not None and d.get("mcap_fin"):
        r2 = fl / d["mcap_fin"] * 100
        cc = d.get("capex_chg")
        t2 = f"{money(fl, d.get('fin_currency') or d.get('currency', 'USD'))} " \
             f"(시총대비 {r2:+.1f}%)"
        if fl < 0 and cc is not None and cc >= 50:
            t2 += f" · 설비투자 {cc:+.0f}%, 증설기"
        o.append(("잉여현금흐름",
                  10 if r2 >= 5 else 8 if r2 >= 2 else 5 if r2 >= 0
                  else 3 if r2 >= -3 else 1 if r2 >= -8 else 0, t2))
    r_ = d.get("rnd")
    if r_ is not None:
        o.append(("R&D 집중도",
                  5 if r_ >= 20 else 4 if r_ >= 10 else 3 if r_ >= 5
                  else 1 if r_ > 0 else 0,
                  f"매출의 {r_:.1f}%" if r_ > 0 else "R&D 거의 없음"))
    dd_ = d["dd"]
    if dd_ is not None:
        o.append(("하락 회복력", 2 if dd_ >= -10 else 4 if dd_ >= -25 else 5 if dd_ >= -45
                  else 3 if dd_ >= -65 else 1, f"고점대비 {dd_:.0f}%"))
    return o


def score_lt(d):
    import statistics as stx
    o = []
    f = d["fcfs"]
    if f and len(f) >= 3:
        pos = sum(1 for x in f if x > 0)
        o.append(("FCF 안정성", 20 if pos == len(f) and f[-1] >= f[0]
                  else 15 if pos == len(f) else 8 if pos >= len(f)-1 else 2,
                  f"{pos}/{len(f)}년 흑자"))
    m = d["margins"]
    if m and len(m) >= 3:
        avg, sd = stx.mean(m), stx.pstdev(m)
        t = " → ".join(f"{x:.0f}" for x in m)
        if avg <= 0:
            o.append(("이익률 안정성", 0, f"[{t}%] 적자"))
        else:
            cv = sd / abs(avg)
            o.append(("이익률 안정성", 15 if cv < .15 else 12 if cv < .3
                      else 7 if cv < .5 else 3, f"[{t}%]"))
    r = d["roes"]
    if r and len(r) >= 3:
        avg, lo = stx.mean(r), min(r)
        t = " → ".join(f"{x:.0f}" for x in r)
        o.append(("자본수익률", 15 if avg >= 20 and lo >= 12 else 12 if avg >= 15 and lo >= 8
                  else 8 if avg >= 10 else 4 if avg >= 5 else 0, f"[{t}%]"))
    rv = d["revs"]
    if rv and len(rv) >= 3:
        drops = sum(1 for a, b in zip(rv, rv[1:]) if b < a)
        worst = min((b/a-1)*100 for a, b in zip(rv, rv[1:]))
        o.append(("침체 생존력", 15 if drops == 0 else 11 if worst > -10
                  else 6 if worst > -25 else 2,
                  f"역성장 {drops}회" + (f", 최악 {worst:.0f}%" if drops else "")))
    c = d["cagr"]
    if c is not None:
        o.append(("장기 성장률", 15 if c >= 20 else 13 if c >= 12 else 10 if c >= 7
                  else 6 if c >= 3 else 2 if c >= 0 else 0, f"연 {c:.1f}%"))
    v = d["dil_annual"]
    if v is not None:
        base = (10 if v <= -3 else 8 if v <= -1 else 6 if v <= 1
                else 3 if v <= 4 else 0)
        note, dc = "", d.get("debt_chg")
        de_now = d.get("debt")
        if de_now is not None and de_now < 30:
            dc = None
        if v <= -1 and dc is not None and dc >= 50:
            base = min(base, 5)
            note = f" · 부채 {dc:+.0f}% 동반"
        o.append(("주식수 관리", base, f"{v:+.1f}%/년{note}"))
    dy_, dyr = d.get("dy"), d.get("div_yrs")
    if dy_ is not None:
        y = dy_                      # fetch 에서 이미 % 단위로 맞춰 둠
        o.append(("배당",
                  10 if (dyr and dyr >= 10) else 8 if (dyr and dyr >= 5)
                  else 6 if y >= 2 else 4 if y > 0 else 2,
                  (f"{y:.2f}% · {dyr}년 연속 인상" if dyr else
                   (f"{y:.2f}%" if y > 0 else "무배당 (성장 재투자형)"))))
    de, cov = d["debt"], d["cover"]
    if de is not None:
        t = f"부채 {de:.0f}%" + (f", 이자보상 {cov:.1f}배" if cov else "")
        if cov is not None and cov <= 0:      # 영업적자 = 이자를 못 갚는 상태
            o.append(("부채 안전성", 3 if de < 50 else 2 if de < 100 else 0,
                      t + " · 영업적자"))
        else:
            o.append(("부채 안전성", 10 if de < 50 and (cov is None or cov > 5)
                      else 7 if de < 100 else 4 if de < 200 else 1, t))
    per = d["per"]
    if per and per > 0:
        base = 8 if (c or 0) >= 15 else 5
        o.append(("밸류에이션", 10 if per < 15 else base+1 if per < 25
                  else base-2 if per < 40 else 2 if per < 60 else 0, f"PER {per:.1f}"))
    return o


# ═════════════════════════════════════════════════════════════
# 수집
# ═════════════════════════════════════════════════════════════

@cache(900)
def fetch(t):
    tk = yf.Ticker(t)
    try:
        info = tk.info or {}
    except Exception:
        info = {}
    if not (info.get("shortName") or info.get("longName")):
        return None

    pct = lambda v: v*100 if isinstance(v, (int, float)) else None
    cur = info.get("currency", "USD")
    fx = USD_KRW if cur == "USD" else 1
    mcap = info.get("marketCap")

    inc, bs, cf = tk.income_stmt, tk.balance_sheet, tk.cashflow
    qi, qb, qc = tk.quarterly_income_stmt, tk.quarterly_balance_sheet, tk.quarterly_cashflow

    # 분기 이익률
    qmargin = []
    qr, qo = _row(qi, ["Total Revenue"]), _row(qi, ["Operating Income", "EBIT"])
    if qr is not None and qo is not None:
        for dt in list(qr.index)[::-1]:
            if dt in qo.index and float(qr[dt]) > 0:
                qmargin.append(float(qo[dt])/float(qr[dt])*100)

    # 분기 매출 YoY
    rev1y = None
    if qr is not None and len(qr) >= 5:
        v = list(qr.values)[::-1]
        if float(v[4]) > 0:
            rev1y = (float(v[0])/float(v[4])-1)*100

    # 분기 매출 YoY 시퀀스 (성장 가속도용, 최신순)
    qgrowth = []
    if qr is not None and len(qr) >= 5:
        v = list(qr.values)[::-1]
        for i in range(len(v) - 4):
            try:
                cur_, yoy_ = float(v[i]), float(v[i + 4])
                if yoy_ > 0:
                    qgrowth.append((cur_ / yoy_ - 1) * 100)
            except Exception:
                pass

    # 연간
    rev = _row(inc, ["Total Revenue"])
    revs = [float(x) for x in rev.values] if rev is not None else []
    op = _row(inc, ["Operating Income", "EBIT"])
    ni_a = _row(inc, ["Net Income"])
    eq = _row(bs, ["Stockholders Equity"])
    # 야후는 회사마다 영업현금흐름 행 이름이 다르다.
    # ASML 은 "Operating Cash Flow" 가 없어 항목이 통째로 빠졌었다.
    ocf_a = _row(cf, ["Operating Cash Flow",
                       "Cash Flow From Continuing Operating Activities",
                       "Cash Flowsfromusedin Operating Activities Direct",
                       "Net Cash Provided By Used In Operating Activities",
                       "Total Cash From Operating Activities"])
    capex = _row(cf, ["Capital Expenditure",
                      "Purchase Of PPE",
                      "Net PPE Purchase And Sale"])
    intx = _row(inc, ["Interest Expense"])
    sh = _row(bs, ["Ordinary Shares Number", "Share Issued"])

    margins = []
    if rev is not None and op is not None:
        for dt in rev.index:
            if dt in op.index and float(rev[dt]) > 0:
                margins.append(float(op[dt])/float(rev[dt])*100)
    roes = []
    if ni_a is not None and eq is not None:
        for dt in ni_a.index:
            if dt in eq.index and float(eq[dt]) > 0:
                roes.append(float(ni_a[dt])/float(eq[dt])*100)
    fcfs = []
    if ocf_a is not None:
        for dt in ocf_a.index:
            c_ = float(capex[dt]) if (capex is not None and dt in capex.index) else 0.0
            fcfs.append(float(ocf_a[dt])+c_)

    # 최신 연도 잉여현금흐름(FCF) = 영업현금흐름 - 설비투자
    # 증설기 회사는 이익이 나도 FCF 가 크게 마이너스인 경우가 있어 따로 본다.
    fcf_last = ocf_last = capex_last = capex_chg = None
    try:
        if ocf_a is not None:
            oi = ocf_a.dropna().sort_index()
            ocf_last = float(oi.iloc[-1])
            c_ = 0.0
            if capex is not None:
                ci = capex.dropna().sort_index()
                if len(ci):
                    capex_last = abs(float(ci.iloc[-1]))
                    c_ = float(ci.iloc[-1])
                    if len(ci) >= 2 and abs(float(ci.iloc[-2])) > 0:
                        capex_chg = (capex_last / abs(float(ci.iloc[-2])) - 1) * 100
            fcf_last = ocf_last + c_
    except Exception:
        pass

    # R&D 집중도 (매출 대비 %)
    rnd = None
    rd_ = _row(inc, ["Research And Development"])
    if rd_ is not None and rev is not None:
        try:
            rv = float(rev.iloc[-1])
            if rv > 0:
                rnd = float(rd_.iloc[-1]) / rv * 100
        except Exception:
            pass

    # 배당: 수익률 + 연속 인상 연수
    # ── 배당수익률 ──
    # 두 가지 함정이 있다.
    #  (1) yfinance 버전마다 dividendYield 단위가 다르다 (비율 vs %)
    #  (2) 해외 ADR 은 배당금이 본국 통화, 주가는 달러라
    #      그냥 나누면 환율 배수만큼 부풀려진다.
    #      예: TSM 은 배당이 대만달러라 6.00% 로 나왔다. 실제는 1%대.
    # 그래서 두 경로로 각각 구한 뒤, 크게 어긋나면 작은 쪽을 믿는다.
    dy = None
    price_ = info.get("currentPrice") or info.get("regularMarketPrice")

    dy_calc = None                      # 배당금 ÷ 주가
    tdr = info.get("trailingAnnualDividendRate")
    if isinstance(tdr, (int, float)) and tdr > 0 and price_:
        dy_calc = tdr / price_ * 100

    dy_field = None                     # 야후가 준 값
    raw = info.get("dividendYield")
    if isinstance(raw, (int, float)) and raw > 0:
        dy_field = raw * 100 if raw < 0.5 else raw

    if dy_calc is not None and dy_field is not None:
        # 1.5배 넘게 벌어지면 통화가 섞인 것으로 보고 작은 쪽을 쓴다
        big, small = max(dy_calc, dy_field), min(dy_calc, dy_field)
        dy = small if (small > 0 and big / small > 1.5) else dy_field
    elif dy_field is not None:
        dy = dy_field
    elif dy_calc is not None:
        dy = dy_calc
    elif info.get("payoutRatio") is not None:
        dy = 0.0

    # 그래도 말이 안 되는 값(연 15% 초과)은 버린다.
    # 리츠·특수 배당주가 아닌 이상 나오기 어려운 수치다.
    if dy is not None and dy > 15:
        dy = None
    div_yrs = None
    try:
        dv = tk.dividends
        if dv is not None and len(dv) > 0:
            yearly = dv.groupby(dv.index.year).sum()
            yrs, vals_ = list(yearly.index), list(yearly.values)
            n = 0
            for i in range(len(vals_) - 1, 0, -1):
                if vals_[i] > vals_[i - 1]:
                    n += 1
                else:
                    break
            div_yrs = n if n else None
    except Exception:
        pass

    cagr = None
    if len(revs) >= 2 and revs[0] > 0 and revs[-1] > 0:
        cagr = ((revs[-1]/revs[0])**(1/(len(revs)-1))-1)*100

    # 연평균 주식수 증감(%). 컬럼 순서를 날짜로 정렬해서 계산한다.
    dil_annual = None
    if sh is not None and len(sh) >= 2:
        try:
            ss = sh.dropna().sort_index()           # 오래된 → 최신
            a, b = float(ss.iloc[0]), float(ss.iloc[-1])
            if a > 0 and len(ss) >= 2:
                dil_annual = ((b / a) ** (1 / (len(ss) - 1)) - 1) * 100
        except Exception:
            pass

    # 분기 주식수 희석
    # 주식수 1년 변화(%). yfinance 는 분기 컬럼 순서가 뒤죽박죽이라
    # 날짜로 정렬한 뒤 "가장 최근" 대 "약 1년 전"을 비교한다.
    dilution = None
    qs = _row(qb, ["Ordinary Shares Number", "Share Issued"])
    if qs is not None and len(qs) >= 2:
        try:
            ser = qs.dropna().sort_index()          # 오래된 → 최신
            v = [float(x) for x in ser.values][::-1]  # 최신 → 오래된
            i = 4 if len(v) > 4 else len(v) - 1
            if v[i] > 0:
                dilution = (v[0] / v[i] - 1) * 100
        except Exception:
            pass
    # 분기 데이터가 부실하면 연간 추세로 대체
    if dilution is None and dil_annual is not None:
        dilution = dil_annual

    # 분기 현금흐름 질
    ocf_q = _row(qc, ["Operating Cash Flow",
                       "Cash Flow From Continuing Operating Activities",
                       "Cash Flowsfromusedin Operating Activities Direct",
                       "Net Cash Provided By Used In Operating Activities",
                       "Total Cash From Operating Activities"])
    ni_q = _row(qi, ["Net Income"])
    ocf_s = float(ocf_q.iloc[-4:].sum()) if ocf_q is not None else None
    ni_s = float(ni_q.iloc[-4:].sum()) if ni_q is not None else None

    # 이자보상배율 = 영업이익 / 이자비용 (최신 연도 기준)
    # yfinance 는 컬럼이 오래된순/최신순으로 뒤바뀌는 경우가 있어 인덱스로 맞춘다
    cover = None
    if op is not None and intx is not None:
        try:
            dt_ = max(set(op.index) & set(intx.index))
            i_ = abs(float(intx[dt_]))
            if i_ > 0:
                cover = float(op[dt_]) / i_
        except Exception:
            pass

    # 부채비율 = 총부채 / 자기자본.
    # ★ 반드시 "최신 분기" 기준으로 본다. 연간 데이터는 최대 1년 묵어서
    #   차입 자사주매입·인수 같은 큰 변화를 놓친다.
    debt_ratio = info.get("debtToEquity")
    debt_asof = None
    for df_, tag in ((qb, "분기"), (bs, "연간")):
        try:
            td = _row(df_, ["Total Debt"])
            eq_ = _row(df_, ["Stockholders Equity"])
            if td is None or eq_ is None:
                continue
            common = set(td.dropna().index) & set(eq_.dropna().index)
            if not common:
                continue
            dt2 = max(common)
            e_ = float(eq_[dt2])
            if e_ > 0:
                debt_ratio = float(td[dt2]) / e_ * 100
                debt_asof = f"{tag} {str(dt2)[:10]}"
                break
        except Exception:
            continue

    # 주당 장부가(BVPS) 증감(%) — 증자가 실제로 주주가치를 깎았는지 판별.
    # 자기자본 총액은 이익이 쌓여도 늘기 때문에 판별 기준이 못 된다.
    # 주식수로 나눈 "주당" 장부가가 줄었는지를 봐야 한다.
    bvps_chg = None
    try:
        if eq is not None and sh is not None:
            ei = eq.dropna().sort_index()
            si = sh.dropna().sort_index()
            common = [x for x in ei.index if x in si.index]
            if len(common) >= 2:
                a_ = float(ei[common[-2]]) / float(si[common[-2]])
                b_ = float(ei[common[-1]]) / float(si[common[-1]])
                if a_ > 0:
                    bvps_chg = (b_ / a_ - 1) * 100
    except Exception:
        pass

    # 부채 증감(%) — 자사주 소각의 재원이 빚인지 판별하는 데 씀
    debt_chg = None
    try:
        td_q = _row(qb, ["Total Debt"])
        if td_q is not None:
            dv = [float(x) for x in td_q.dropna().sort_index().values][::-1]
            if len(dv) >= 2:
                j = 4 if len(dv) > 4 else len(dv) - 1
                if dv[j] > 0:
                    debt_chg = (dv[0] / dv[j] - 1) * 100
    except Exception:
        pass

    # ── 재무제표 통화 기준 시가총액 ──
    # 해외 ADR 은 재무제표가 본국 통화(TSM=대만달러)인데 주가는 달러다.
    # "FCF / 시총" 같은 비율을 그냥 계산하면 환율 배수만큼 틀린다.
    #   예: TSM 잉여현금흐름이 시총의 44% 로 나왔다. 실제는 1.4%.
    # 그래서 시가총액을 재무제표 통화로 환산해 둔다.
    fin_cur = info.get("financialCurrency") or cur
    mcap_fin = mcap
    if mcap and fin_cur != cur:
        rate = None
        try:
            fx_t = yf.Ticker(f"{cur}{fin_cur}=X")
            h_fx = fx_t.history(period="5d")
            if h_fx is not None and len(h_fx):
                rate = float(h_fx["Close"].iloc[-1])
        except Exception:
            pass
        mcap_fin = mcap * rate if rate else None   # 못 구하면 계산 포기

    # 순현금 = 현금 - 총부채.
    # 분기 컬럼 순서가 뒤바뀌어 오는 경우가 있어 날짜로 정렬한 뒤
    # 가장 최근 값을 쓴다. (주식수·부채비율에서 같은 문제를 이미 겪었다)
    netcash = None
    try:
        def _latest(df_, keys):
            r_ = _row(df_, keys)
            if r_ is None:
                return None
            ser = r_.dropna().sort_index()
            return float(ser.iloc[-1]) if len(ser) else None

        cash = _latest(qb, ["Cash And Cash Equivalents",
                            "Cash Cash Equivalents And Short Term Investments"])
        debt_q = _latest(qb, ["Total Debt"])
        if cash is None:                      # 분기에 없으면 연간으로
            cash = _latest(bs, ["Cash And Cash Equivalents",
                                "Cash Cash Equivalents And Short Term Investments"])
        if debt_q is None:
            debt_q = _latest(bs, ["Total Debt"])
        if cash is not None:
            netcash = cash - (debt_q or 0)
    except Exception:
        pass

    px1y, dd = None, None
    try:
        h = tk.history(period="2y")
        if h is not None and not h.empty:
            c_ = h["Close"]
            if len(c_) > 250:
                px1y = (float(c_.iloc[-1])/float(c_.iloc[-250])-1)*100
            dd = float((c_/c_.cummax()-1).iloc[-1]*100)
    except Exception:
        pass

    beats = (0, 0)
    try:
        h_ = tk.earnings_history
        if h_ is not None and not pd.DataFrame(h_).empty:
            df = pd.DataFrame(h_)
            ec = next((c for c in df.columns if "estimate" in str(c).lower()), None)
            ac = next((c for c in df.columns if "actual" in str(c).lower()
                       or "reported" in str(c).lower()), None)
            if ec and ac:
                s = df[[ec, ac]].dropna().tail(8)
                if not s.empty:
                    beats = (int((s[ac] > s[ec]).sum()), len(s))
    except Exception:
        pass

    return {
        "ticker": t.upper(),
        "name": info.get("shortName") or info.get("longName"),
        "sector": info.get("sector"),
        "price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "chg": info.get("regularMarketChangePercent"),
        "currency": cur,
        "mcap_raw": mcap, "mcap_krw": mcap*fx if mcap else None,
        "mcap_fin": mcap_fin, "fin_currency": fin_cur,
        "fx_note": (None if fin_cur == cur else
                    (f"{cur}→{fin_cur} 환산됨" if mcap_fin
                     else f"{cur}→{fin_cur} 환율 조회 실패")),
        "margin": pct(info.get("operatingMargins")),
        "debt": debt_ratio, "debt_asof": debt_asof, "debt_chg": debt_chg,
        "debt_yf": info.get("debtToEquity"),
        "insider": pct(info.get("heldPercentInsiders")),
        "inst": pct(info.get("heldPercentInstitutions")),
        "roe": pct(info.get("returnOnEquity")),
        "per": info.get("trailingPE") or info.get("forwardPE"),
        "peg": info.get("pegRatio") or info.get("trailingPegRatio"),
        "growth": cagr,
        "qmargin": qmargin, "rev1y": rev1y, "px1y": px1y, "dd": dd,
        "revs": revs, "margins": margins, "roes": roes, "fcfs": fcfs,
        "cagr": cagr, "dil_annual": dil_annual, "dilution": dilution,
        "ocf": ocf_s, "ni": ni_s, "cover": cover, "netcash": netcash,
        "beats": beats,
        "qgrowth": qgrowth, "rnd": rnd, "dy": dy, "div_yrs": div_yrs,
        "fcf_last": fcf_last, "ocf_last": ocf_last,
        "capex_last": capex_last, "capex_chg": capex_chg, "bvps_chg": bvps_chg,
        "tgt": info.get("targetMeanPrice"),
        "n_analyst": info.get("numberOfAnalystOpinions"),
        "rec": info.get("recommendationKey"),
        "short_pct": pct(info.get("shortPercentOfFloat")),
        "earnings": info.get("earningsTimestamp"),
    }


def money(v, cur="USD"):
    """종목 통화에 맞춰 금액을 읽기 쉽게. USD=M/B, KRW=억/조."""
    if v is None:
        return "-"
    if cur == "KRW":
        a = abs(v)
        if a >= 1_0000_0000_0000:
            return f"{v/1_0000_0000_0000:,.2f}조원"
        if a >= 1_0000_0000:
            return f"{v/1_0000_0000:,.0f}억원"
        return f"{v:,.0f}원"
    a = abs(v)
    if a >= 1e9:
        return f"{v/1e9:,.2f}B"
    if a >= 1e6:
        return f"{v/1e6:,.0f}M"
    return f"{v:,.0f}"


def won(n):
    if n is None:
        return "-"
    return f"{n/조:.1f}조" if n >= 조 else f"{n/억:,.0f}억"


def pctile(items, mx):
    got = sum(s for _, s, _ in items)
    avail = sum(mx[k] for k, _, _ in items)
    return got, avail, (got/avail*100 if avail else 0)


def ten_verdict(p):
    if p >= 70: return "후보군", ORANGE
    if p >= 52: return "관찰", AMBER
    if p >= 35: return "보류", "#6B7280"
    return "제외", "#9CA3AF"


def lt_verdict(p):
    if p >= 75: return "핵심 보유", ORANGE
    if p >= 58: return "보유 적합", AMBER
    if p >= 40: return "조건부", "#6B7280"
    return "부적합", "#9CA3AF"


def dday(ts):
    if not ts:
        return None, None
    try:
        e = datetime.fromtimestamp(ts)
        return e, (e - datetime.now()).days
    except Exception:
        return None, None




# ═════════════════════════════════════════════════════════════
# 수급 동향 (가격/거래량 6개월 기반, 0~100)
#   70↑ 매수 우위 / 40~70 중립 / 40↓ 매도 우위
# ═════════════════════════════════════════════════════════════

@cache(3600)
def footprint(t):
    try:
        h = yf.Ticker(t).history(period="6mo", auto_adjust=True)
    except Exception:
        return None
    return footprint_from(h)


def footprint_from(h):
    """가격·거래량 데이터프레임에서 수급 점수 계산.
    백테스트에서 과거 시점 데이터를 잘라 넣을 수 있도록 분리해 두었다."""
    if h is None or len(h) < 60:
        return None

    c, v = h["Close"], h["Volume"].astype(float)
    ret = c.pct_change()
    v20 = v.rolling(20).mean()

    # 1) 대량거래일: 최근 20일 중 거래량 > 20일평균 x2, 양봉/음봉 구분
    last20 = h.tail(20)
    spike = last20[v.tail(20) > (v20.tail(20) * 2)]
    spike_up = int((spike["Close"] > spike["Open"]).sum())
    spike_dn = int(len(spike)) - spike_up

    # 2) 상승일 거래량 / 하락일 거래량 (20일)
    up_v = v.tail(20)[ret.tail(20) > 0].sum()
    dn_v = v.tail(20)[ret.tail(20) < 0].sum()
    updn = float(up_v / dn_v) if dn_v > 0 else 3.0

    # 3) OBV 다이버전스 (20일)
    sign = ret.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    obv = (v * sign).cumsum()
    obv_chg = float((obv.iloc[-1] - obv.iloc[-21]) / (v20.iloc[-1] * 20))
    px_chg = float(c.iloc[-1] / c.iloc[-21] - 1)
    diverge = obv_chg > 0.15 and px_chg < 0.03      # 가격 횡보 + OBV↑ = 조용한 매수 누적
    distrib = obv_chg < -0.15 and px_chg > -0.03    # 가격 버팀 + OBV↓ = 조용한 매도 누적

    # 4) 눌림목: 50MA 위 + 20MA ±5% + 거래량 마름
    ma20 = float(c.rolling(20).mean().iloc[-1])
    ma50 = float(c.rolling(50).mean().iloc[-1])
    px = float(c.iloc[-1])
    dry = float(v.tail(5).mean()) < float(v20.iloc[-1]) * 0.7
    pullback = px > ma50 and abs(px / ma20 - 1) < 0.05 and dry

    # 5) 6개월 고점 대비
    off_hi = px / float(c.max()) - 1

    # ── 점수 산출 ──
    # 예전에는 "조건을 만족하면 +15" 식의 고정 가감점이었다.
    # 그러면 서로 다른 종목이 똑같은 점수(32, 50, 58 ...)로 뭉치는 문제가 있어
    # 각 지표를 연속값으로 환산해 가중 평균하는 방식으로 바꿨다.

    def squash(x, lo, hi):
        """x 를 lo~hi 구간에서 0~1 로. 범위를 벗어나면 0 또는 1."""
        if hi == lo:
            return 0.5
        return max(0.0, min(1.0, (x - lo) / (hi - lo)))

    parts = []          # (0~1 점수, 가중치)

    # ① 상승일 대비 하락일 거래량비 — 0.6배=0, 1.0배=0.5, 1.8배=1
    parts.append((squash(updn, 0.6, 1.8), 30))

    # ② OBV 20일 변화 — 평균거래량 대비. -0.4=0, 0=0.5, +0.4=1
    parts.append((squash(obv_chg, -0.4, 0.4), 25))

    # ③ 가격 대비 OBV 괴리 — 가격은 안 움직이는데 OBV 가 움직이면 강한 신호
    gap = obv_chg - px_chg * 2.5
    parts.append((squash(gap, -0.5, 0.5), 20))

    # ④ 대량거래일 방향 — 양봉과 음봉의 비율
    tot_spike = spike_up + spike_dn
    if tot_spike:
        parts.append((spike_up / tot_spike, 15))
    else:
        parts.append((0.5, 5))       # 대량거래가 없으면 중립, 비중도 낮춤

    # ⑤ 추세 위치 — 20MA/50MA 이격과 거래량 마름
    trend = 0.5
    if ma50 > 0:
        trend = squash(px / ma50 - 1, -0.25, 0.15)
    if pullback:
        trend = min(1.0, trend + 0.15)
    parts.append((trend, 10))

    wsum = sum(w for _, w in parts)
    raw = sum(p * w for p, w in parts) / wsum
    score = int(round(raw * 100))

    # 고점 대비 크게 빠진 채 매도세가 우세하면 추가 감점 (연속)
    if off_hi < -0.25 and updn < 1.0:
        score -= int(round(min(12, (-off_hi - 0.25) * 40)))
    score = max(0, min(100, score))

    return dict(score=score, spike_up=spike_up, spike_dn=spike_dn, updn=updn,
                obv_chg=obv_chg, px_chg=px_chg, diverge=diverge, distrib=distrib,
                pullback=pullback, off_hi=off_hi, ma20=ma20, ma50=ma50)



# ═════════════════════════════════════════════════════════════
# 차트용 일봉 데이터
#   footprint 와 같은 6개월 일봉을 쓰지만, 그리기용으로 따로 뽑는다.
#   (footprint 는 점수만 반환하고 원본을 버리기 때문)
# ═════════════════════════════════════════════════════════════

@cache(3600)
def chart_data(t, period="1y"):
    """일봉 + 이동평균 + 기간별 수익률. 실패 시 None."""
    try:
        h = yf.Ticker(t).history(period=period, auto_adjust=True)
    except Exception:
        return None
    if h is None or len(h) < 30:
        return None

    c = h["Close"]
    ma20 = c.rolling(20).mean()
    ma50 = c.rolling(50).mean()

    def ret(days):
        """N거래일 전 대비 수익률.
        데이터가 조금 모자라면(예: 1년치인데 248일) 가장 오래된 값을 쓴다.
        너무 모자라면(절반 미만) None."""
        if len(c) < 2:
            return None
        idx = len(c) - 1 - days
        if idx < 0:
            if len(c) < days * 0.5:
                return None
            idx = 0
        a, b = float(c.iloc[idx]), float(c.iloc[-1])
        return (b / a - 1) * 100 if a > 0 else None

    rows = []
    for i, (dt, row) in enumerate(h.iterrows()):
        rows.append({
            "date": str(dt)[:10],
            "open": float(row["Open"]), "high": float(row["High"]),
            "low": float(row["Low"]), "close": float(row["Close"]),
            "volume": float(row["Volume"]),
            "ma20": (float(ma20.iloc[i]) if ma20.iloc[i] == ma20.iloc[i] else None),
            "ma50": (float(ma50.iloc[i]) if ma50.iloc[i] == ma50.iloc[i] else None),
        })

    return {
        "rows": rows,
        "ret_7d": ret(5), "ret_1m": ret(21),
        "ret_3m": ret(63), "ret_6m": ret(126), "ret_1y": ret(251),
        "hi": float(c.max()), "lo": float(c.min()),
        "last": float(c.iloc[-1]),
    }


def fp_verdict(score):
    if score >= 70:
        return "매수 우위", ORANGE
    if score <= 40:
        return "매도 우위", "#2563EB"
    return "중립", "#9CA3AF"



# ═════════════════════════════════════════════════════════════
# 레이더 차트 (5축) — 세부 항목을 5개 축으로 묶어서 SVG로 그림
# ═════════════════════════════════════════════════════════════

AXES_TEN = [
    ("성장성",   ["성장 가속도", "이익률 추세", "어닝 서프라이즈", "실적-주가 괴리"]),
    ("수익성",   ["영업이익률", "ROE", "이익의 질", "잉여현금흐름"]),
    ("재무안전", ["부채비율", "순현금/시총", "주식수 희석"]),
    ("주가매력", ["밸류에이션", "하락 회복력"]),
    ("규모 여력", ["시가총액", "내부자지분", "R&D 집중도"]),
]

AXES_LT = [
    ("현금창출", ["FCF 안정성"]),
    ("안정성",   ["이익률 안정성", "침체 생존력"]),
    ("자본효율", ["자본수익률"]),
    ("성장성",   ["장기 성장률"]),
    ("주주환원", ["주식수 관리", "배당", "부채 안전성", "밸류에이션"]),
]
