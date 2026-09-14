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

# ── 다모다란 관점 점수 (100점) ──
#   기존 두 채점과는 별개다. SCORE_VERSION 에 영향을 주지 않는다.
#   "지금 재무가 좋은가" 가 아니라
#   "자본을 굴려 가치를 만들고 있는가" 를 본다.
#   앞 두 항목에 60점을 몰았다. 다모다란이 가장 중요하게 보는 것이라서.
DAMO_MAX = {"ROIC 초과수익": 35, "재투자 효율": 25, "이익률 추세": 20,
            "부채 안전성": 10, "함정 없음": 10}
DAMO_WACC = 9.0          # 자본비용 기본 가정 %

# R&D 자본화 상각연수 (년)
#   ★ 모든 종목에 똑같이 적용해야 한다.
#     종목마다 다르게 잡으면 조정 강도가 달라져 순위 비교가 오염된다.
#     처음엔 "가진 연수만큼(최대 5년)" 으로 만들었다가,
#     어떤 종목은 4년 어떤 종목은 5년이 되어 비교가 깨지는 걸 보고 고정했다.
#
#   4로 둔 이유는 야후 연간 손익이 대개 4년치이기 때문이다.
#   다모다란은 반도체에 5년을 쓰므로, 우리 값은 "조금 덜 조정된" 값이다.
#   ANET 기준 3년 25.5% / 4년 25.2% / 5년 24.9% 로
#   연수를 바꿔도 결과가 크게 흔들리지는 않는다(0.6%p).
RND_LIFE = 4


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
        # 음수 = 자기자본이 음수(자본잠식). 기준이 전부 "작을수록 좋다" 라
        # 그냥 두면 최악의 회사가 만점을 받는다. (2026-09-13)
        if v < 0:
            o.append(("부채비율", 0, f"자본잠식 ({v:.0f}%){asof}"))
        else:
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

    # PEG 가 지나치게 낮은 것은 대개 적자→흑자 전환 때
    # 성장률이 수천 % 로 튀어서 생긴 값이다.
    #   AEHR PEG 0.02 (PER 43.7), ALAB PEG 0.02 (PER 141)
    # 일회성 기저효과를 "싸다" 로 읽으면 안 되므로
    # PEG 0.2 미만이면 믿지 않고 PER 절대수준으로 평가한다.
    peg_ok = bool(peg) and peg > 0.2
    # PEG 를 믿을 수 없으면 같은 이익에서 나온 성장률도 믿지 않는다.
    # (ALAB 은 PEG 0.02 를 걸러도 성장률 120% 로 다시 후한 점수를 받았다)
    g_ok = (bool(g) and 0 < g <= 80
            and not (bool(peg) and peg <= 0.2))

    if peg_ok:
        o.append(("밸류에이션", 10 if peg < 1 else 8 if peg < 1.5 else 5 if peg < 2.5
                  else 2 if peg < 4 else 0, f"PEG {peg:.2f}"))
    elif per and per > 0 and g_ok:
        r = per / g
        o.append(("밸류에이션", 10 if r < 1 else 8 if r < 1.5 else 5 if r < 2.5 else 2,
                  f"PER {per:.0f} (성장 {g:.0f}%)"))
    elif per and per > 0 and (peg or g):
        # 성장률이나 PEG 는 있는데 믿을 수 없는 값인 경우.
        # PER 절대수준으로만 보고, 그 사실을 화면에 밝힌다.
        why = (f"PEG {peg:.2f}" if peg and peg <= 0.2 else f"성장 {g:.0f}%")
        o.append(("밸류에이션",
                  9 if per < 10 else 7 if per < 15 else 5 if per < 25
                  else 3 if per < 40 else 1 if per < 60 else 0,
                  f"PER {per:.1f} ({why} 는 기저효과로 제외)"))
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
        if de_now is not None and 0 <= de_now < 30:
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
    elif d.get("eq_negative"):
        # ★ 2026-09-13 — roes 는 자기자본이 양수인 해만 담는다.
        #   그래서 자본잠식 회사는 이 항목이 통째로 빠지고,
        #   만점 분모가 15점 줄어 % 가 오히려 올라갔다.
        #   "회사가 나빠서 항목이 빠지고, 그래서 점수가 좋아진다" 는
        #   거꾸로 된 일이라 0점으로 넣어 분모에 남긴다.
        o.append(("자본수익률", 0, "자기자본 잠식"))
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
        if de_now is not None and 0 <= de_now < 30:
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
        if de < 0:                            # 자본잠식 (2026-09-13)
            o.append(("부채 안전성", 0, f"자본잠식 ({de:.0f}%)"))
        elif cov is not None and cov <= 0:     # 영업적자 = 이자를 못 갚는 상태
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


def score_damo(d, wacc=None):
    """다모다란 관점 점수. (항목, 점수, 설명) 목록을 돌려준다.

    기존 채점(score_ten / score_lt)과 독립이다.
    데이터가 없는 항목은 아예 넣지 않으므로,
    pctile() 로 만점 대비 백분율을 내면 된다.
    """
    w = DAMO_WACC if wacc is None else wacc
    o = []

    # ① ROIC 가 자본비용을 얼마나 넘는가 (35점)
    rc = d.get("roic")
    if rc is not None:
        gap = rc - w
        sc = (35 if gap >= 20 else 28 if gap >= 10 else 20 if gap >= 5
              else 12 if gap >= 0 else 0)
        o.append(("ROIC 초과수익", sc,
                  f"{rc:.1f}% (자본비용 {w:.0f}% 대비 {gap:+.1f}%p)"))

    # ② 돈 1 을 써서 매출이 얼마나 늘었나 (25점)
    re_ = d.get("reinv_eff")
    if re_ is not None:
        sc = (25 if re_ >= 2.0 else 20 if re_ >= 1.5 else 15 if re_ >= 1.0
              else 8 if re_ >= 0.5 else 0)
        note = ("효율 높음" if re_ >= 2 else "보통" if re_ >= 0.5
                else "투자 회수 전" if re_ >= 0 else "매출 감소 중")
        o.append(("재투자 효율", sc, f"{re_:.2f}배 · {note}"))

    # ③ 이익률이 개선되는 방향인가 (20점)
    #    적자여도 방향이 맞으면 점수를 준다. 초기 성장주를 보기 위함.
    m = d.get("margins")
    if m and len(m) >= 3:
        late = sum(m[-2:]) / 2
        early = sum(m[:2]) / 2 if len(m) >= 4 else m[0]
        tr = late - early
        sc = (20 if tr >= 10 else 15 if tr >= 3 else 8 if tr >= -3 else 0)
        note = ("개선 중" if tr >= 3 else "횡보" if tr >= -3 else "악화 중")
        o.append(("이익률 추세", sc,
                  f"[{' → '.join(f'{x:.0f}' for x in m[-4:])}%] {tr:+.0f}%p · {note}"))

    # ④ 망하지 않을 여력 (10점)
    de = d.get("debt")
    if de is not None:
        if de < 0:                            # 자본잠식 (2026-09-13)
            o.append(("부채 안전성", 0, f"자본잠식 ({de:.0f}%)"))
        else:
            sc = 10 if de <= 30 else 7 if de <= 60 else 4 if de <= 100 else 0
            o.append(("부채 안전성", sc, f"부채비율 {de:.0f}%"))

    # ⑤ 숫자가 튀어 좋아 보이는 함정 (10점)
    #    PEG 0.2 미만 = 적자→흑자 전환 착시
    #    ROE 100% 초과 = 자기자본이 쪼그라든 결과
    peg, roe = d.get("peg"), d.get("roe")
    traps = []
    if peg is not None and 0 < peg < 0.2:
        traps.append(f"PEG {peg:.2f}")
    if roe is not None and roe > 100:
        traps.append(f"ROE {roe:.0f}%")
    if peg is not None or roe is not None:
        sc = 10 if not traps else 5 if len(traps) == 1 else 0
        o.append(("함정 없음", sc,
                  "없음" if not traps else " / ".join(traps) + " 주의"))

    return o


def roic_gap_text(d, wacc=None):
    """기존 ROIC 와 R&D 자본화 ROIC 를 한 줄로 비교.
    (라벨, 설명, 판정이 뒤집혔는지) 를 돌려준다. 못 구하면 None."""
    w = DAMO_WACC if wacc is None else wacc
    rc, ra = d.get("roic"), d.get("roic_adj")
    if rc is None or ra is None:
        return None
    diff = ra - rc
    # 자본비용을 넘느냐 마느냐가 뒤집히면 그게 제일 중요한 신호다
    flip = (rc >= w) != (ra >= w)
    if flip:
        msg = ("조정 후 자본비용 미달로 바뀜" if rc >= w
               else "조정 후 자본비용 상회로 바뀜")
    elif abs(diff) < 1:
        msg = "차이 거의 없음"
    else:
        msg = f"R&D 자본화하면 {abs(diff):.1f}%p {'낮아짐' if diff < 0 else '높아짐'}"
    return f"{ra:.1f}%", msg, flip


def damo_verdict(p):
    if p >= 75: return "가치 창출형", ORANGE
    if p >= 55: return "양호", AMBER
    if p >= 35: return "주의", "#6B7280"
    return "가치 파괴형", "#9CA3AF"


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
    # ROE 는 자기자본이 양수인 해만 담는다(음수로 나누면 부호가 뒤집힌다).
    # 다만 "자본잠식이라 빠진 것" 과 "데이터가 없어 빠진 것" 은 다르므로
    # 전자를 score_lt 가 알아볼 수 있도록 표시해 둔다. (2026-09-13)
    roes = []
    eq_negative = False
    if ni_a is not None and eq is not None:
        pairs_eq = [dt for dt in ni_a.index if dt in eq.index]
        for dt in pairs_eq:
            if float(eq[dt]) > 0:
                roes.append(float(ni_a[dt])/float(eq[dt])*100)
        eq_negative = bool(pairs_eq) and not roes
    # 잉여현금흐름 = 영업현금흐름 - 설비투자
    #
    # ★ 2026-09-13 수정 — 설비투자 행이 없으면 0 으로 치고 넘어가
    #   FCF 가 OCF 와 같아져 버렸다. 그러면 "설비투자를 안 하는 회사"가
    #   아니라 "설비투자 데이터가 없는 회사"가 FCF 만점을 받는다.
    #   국내 종목처럼 행 이름이 다른 경우에 실제로 생긴다.
    #   같은 해 설비투자가 없으면 그 해를 아예 빼는 쪽으로 고쳤다.
    #
    # ★ 부호도 방어한다. 야후는 Capital Expenditure 를 음수로 주지만
    #   대체 행(Purchase Of PPE 등)이 양수로 올 수 있다.
    #   -abs() 를 쓰면 어느 쪽이 오든 항상 빼진다.
    fcfs = []
    if ocf_a is not None and capex is not None:
        for dt in ocf_a.index:
            if dt not in capex.index:
                continue
            fcfs.append(float(ocf_a[dt]) - abs(float(capex[dt])))

    # 최신 연도 잉여현금흐름(FCF) = 영업현금흐름 - 설비투자
    # 증설기 회사는 이익이 나도 FCF 가 크게 마이너스인 경우가 있어 따로 본다.
    fcf_last = ocf_last = capex_last = capex_chg = None
    try:
        # 설비투자 증감은 따로 구해 둔다 (증설기 판별용)
        ci = capex.dropna().sort_index() if capex is not None else None
        if ci is not None and len(ci):
            capex_last = abs(float(ci.iloc[-1]))
            if len(ci) >= 2 and abs(float(ci.iloc[-2])) > 0:
                capex_chg = (capex_last / abs(float(ci.iloc[-2])) - 1) * 100

        # ① 야후가 Free Cash Flow 행을 직접 주면 그걸 쓴다
        fcf_row = _row(cf, ["Free Cash Flow"])
        if fcf_row is not None:
            fi = fcf_row.dropna().sort_index()
            if len(fi):
                fcf_last = float(fi.iloc[-1])

        # ② 없으면 같은 연도의 영업현금흐름 - 설비투자로 계산한다.
        #    ASML 은 2025년 영업현금흐름이 비어 있어서
        #    2024년 OCF 에서 2025년 capex 를 빼는 일이 있었다.
        #    반드시 연도를 맞춘다.
        if ocf_a is not None:
            oi = ocf_a.dropna().sort_index()
            if len(oi):
                ocf_last = float(oi.iloc[-1])
                if fcf_last is None:
                    # ★ 2026-09-13 — 설비투자 행이 통째로 없으면(ci is None)
                    #   예전에는 c_ = 0.0 이 남아 FCF = OCF 가 됐다.
                    #   이제는 같은 해 설비투자를 못 찾으면 계산을 포기한다.
                    dt_o = oi.index[-1]
                    if ci is not None and dt_o in ci.index:
                        fcf_last = ocf_last - abs(float(ci[dt_o]))
    except Exception:
        pass

    # R&D 집중도 (매출 대비 %)
    # R&D 집중도 — 같은 연도의 R&D 와 매출을 쓴다.
    # (예전엔 각자 .iloc[-1] 이라 최신 R&D 가 NaN 이면 연도가 엇갈렸다)
    rnd = None
    rd_ = _row(inc, ["Research And Development"])
    if rd_ is not None and rev is not None:
        try:
            common_r = [x for x in rd_.index if x in rev.index]
            if common_r:
                dtr = common_r[-1]
                rv = float(rev[dtr])
                if rv > 0:
                    rnd = float(rd_[dtr]) / rv * 100
        except Exception:
            pass

    # 배당: 수익률 + 연속 인상 연수

    # ═══════════════════════════════════════════════════════
    # 참고 지표 (채점에 쓰지 않음, 화면 표시용)
    #   다모다란이 중요하게 보는 두 가지를 야후 데이터로 계산한다.
    #   점수에 넣지 않는 이유: 6개월 실험이 SCORE_VERSION 1 로
    #   진행 중이라 채점 기준을 바꾸면 비교가 깨진다.
    # ═══════════════════════════════════════════════════════

    # ① ROIC = 세후영업이익 / 투입자본
    #    ROE 는 빚을 많이 쓰면 부풀려지지만 ROIC 는 그렇지 않다.
    #    자본비용(보통 8~10%)보다 높아야 가치를 창출하는 것.
    roic = None
    _roic_ebit = _roic_tax = _roic_invested = _roic_dt = None
    try:
        if op is not None and eq is not None:
            oi_ = op.dropna().sort_index()
            eq_ = eq.dropna().sort_index()
            common = [x for x in oi_.index if x in eq_.index]
            if common:
                dt_ = common[-1]
                ebit = float(oi_[dt_])
                # 실효세율: 야후가 주면 쓰고 아니면 21% 가정
                tax = info.get("effectiveTaxRate")
                tax = tax if isinstance(tax, (int, float)) and 0 <= tax < 0.6 else 0.21
                nopat = ebit * (1 - tax)

                equity_ = float(eq_[dt_])
                debt_t = _row(bs, ["Total Debt"])
                # 넓은 라벨(단기투자 포함)을 먼저. 순현금과 같은 순서로 맞춘다.
                cash_t = _row(bs, ["Cash Cash Equivalents And Short Term Investments",
                                   "Cash And Cash Equivalents"])
                d_ = float(debt_t[dt_]) if (debt_t is not None
                                            and dt_ in debt_t.index) else 0.0
                c_ = float(cash_t[dt_]) if (cash_t is not None
                                            and dt_ in cash_t.index) else 0.0
                invested = equity_ + d_ - c_
                if invested > 0:
                    roic = nopat / invested * 100
                # 아래 R&D 자본화 조정에서 다시 쓴다
                _roic_ebit, _roic_tax = ebit, tax
                _roic_invested, _roic_dt = invested, dt_
    except Exception:
        pass

    # ═══════════════════════════════════════════════════════
    # ①-2 R&D 자본화 ROIC  (2026-09-13 추가, 점수에는 안 들어감)
    #
    #   회계는 R&D 를 그해 비용으로 털어 버린다.
    #   그러면 연구개발로 쌓아 올린 것이 자산으로 안 잡히므로
    #     · 투입자본이 실제보다 작게 잡히고
    #     · 그 결과 ROIC 가 실제보다 높게 나온다
    #   R&D 비중이 큰 회사일수록 이 왜곡이 크다.
    #   우리 목록은 반도체·AI 인프라라 R&D 가 매출의 10~20% 다.
    #
    #   다모다란은 R&D 를 설비투자처럼 보고 자본화하라고 한다.
    #   그 방식을 그대로 옮긴 것이다.
    #
    #   계산
    #     상각연수 L = RND_LIFE (모든 종목 동일. 위 상수 주석 참고)
    #     R&D 자산 = Σ RD(t-i) x (L-i)/L        i = 0 .. L-1
    #     상각비   = 최근 L년 R&D 평균           (정상상태 근사)
    #     조정영업이익 = 영업이익 + 올해 R&D - 상각비
    #     조정투입자본 = 투입자본 + R&D 자산
    #     조정 ROIC  = 조정영업이익 x (1-세율) / 조정투입자본
    #
    #   ★ 점수에 넣지 않는다. SCORE_VERSION 은 1 그대로다.
    #     기존 ROIC 와 나란히 놓고 6개월간 지켜본 뒤,
    #     쓸모가 있으면 v2 에 넣는다. (다모다란 점수를 만든 방식과 같다)
    # ═══════════════════════════════════════════════════════
    roic_adj = rnd_asset = rnd_amort = rnd_life = None
    roic_adj_note = None
    try:
        # ★ 투입자본이 0 이하면 기존 ROIC 도 계산하지 않는다(현금이 자본+부채보다
        #   많은 경우). 그런데 조정 쪽은 R&D 자산을 더해 양수로 넘어가면서
        #   "기존 ROIC 는 없는데 조정 ROIC 만 있는" 값이 나왔다.
        #   screen --roic-adj-min 이 그걸 그대로 통과시킨다. 같이 막는다.
        if _roic_invested is None or _roic_invested <= 0:
            roic_adj_note = "ROIC 자체를 못 구함"
        elif rd_ is None:
            # R&D 행이 아예 없는 회사(유틸리티·산업가스 등)는
            # 자본화할 것이 없으므로 조정해도 값이 같다.
            roic_adj, roic_adj_note = roic, "R&D 없음 (조정 불필요)"
        else:
            rs = rd_.dropna().sort_index()          # 오래된 → 최신
            # 기준 연도가 ROIC 와 같아야 한다. 그보다 뒤의 해는 자른다.
            if _roic_dt is not None:
                rs = rs[[x for x in rs.index if x <= _roic_dt]]
            vals = [abs(float(x)) for x in rs.values]
            if len(vals) < RND_LIFE:
                roic_adj_note = (f"R&D 연수 부족 ({len(vals)}년, "
                                 f"{RND_LIFE}년 필요)")
            else:
                # 연수는 종목마다 다르게 잡지 않는다. 위 RND_LIFE 주석 참고.
                L = RND_LIFE
                recent = vals[-L:]                  # 오래된 → 최신
                # 자산: 최신 해는 100%, 한 해 전은 (L-1)/L, ...
                asset = sum(v * (L - i) / L
                            for i, v in enumerate(reversed(recent)))
                amort = sum(recent) / L
                adj_ebit = _roic_ebit + recent[-1] - amort
                adj_inv = _roic_invested + asset
                if adj_inv > 0:
                    roic_adj = adj_ebit * (1 - _roic_tax) / adj_inv * 100
                    rnd_asset, rnd_amort, rnd_life = asset, amort, L
    except Exception:
        pass

    # ② 재투자 효율 = 매출 증가분 / (설비투자 + R&D)
    #    같은 돈을 써서 매출을 얼마나 늘렸나.
    #    적자 회사도 계산되므로 초기 성장주를 볼 수 있다.
    reinv_eff = None
    try:
        if rev is not None:
            rv = rev.dropna().sort_index()
            if len(rv) >= 2:
                d_rev = float(rv.iloc[-1]) - float(rv.iloc[-2])
                dt_ = rv.index[-1]
                spend = 0.0
                if capex is not None and dt_ in capex.index:
                    spend += abs(float(capex[dt_]))
                if rd_ is not None and dt_ in rd_.index:
                    spend += abs(float(rd_[dt_]))
                if spend > 0:
                    reinv_eff = d_rev / spend
    except Exception:
        pass

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
        # 야후는 같은 필드를 어떤 종목엔 비율(0.0069)로,
        # 어떤 종목엔 퍼센트(0.69)로 준다. 구분할 방법이 없어
        # 배당금과 주가로 직접 계산한 값을 기준으로 판정한다.
        #   PAYC  0.0069 → 0.69%   (100 곱해야 함)
        #   하이닉스 0.08 → 0.08%  (이미 퍼센트)
        cand = (raw, raw * 100)
        if dy_calc and dy_calc > 0:
            # 직접 계산값에 가까운 쪽을 고른다
            dy_field = min(cand, key=lambda x: abs(x - dy_calc))
        else:
            # 배당금이나 주가가 없어 직접 계산을 못 하는 경우.
            #
            # ★ 2026-09-13 수정 — 예전엔 그대로 퍼센트로 봤는데,
            #   야후가 비율(0.0069)로 준 종목은 0.0069% 가 되어
            #   100 배 작게 나왔다. (1번 버그와 정반대 방향)
            #   크기로 판정한다. 0.25 미만이면 비율로 보는 게 맞다.
            #   연 25% 배당은 현실에 거의 없고, 아래 15% 상한이
            #   잘못 곱해진 경우를 한 번 더 걸러 준다.
            dy_field = raw * 100 if raw < 0.25 else raw

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
    # 연속 배당인상 연수
    #
    # ★ 2026-09-13 수정 — 진행 중인 올해를 그대로 넣고 있었다.
    #   9월이면 분기배당 4번 중 3번만 찍혔으니 올해 합계가 작년보다 작고,
    #   첫 번째 비교에서 바로 break 되어 항상 None 이 나왔다.
    #   11년 연속 인상한 회사도 배당 4/10 을 받고,
    #   1월에 돌리면 10/10 을 받는다. 점수가 스캔 날짜에 따라 달라졌다.
    #   올해는 빼고 센다.
    div_yrs = None
    try:
        dv = tk.dividends
        if dv is not None and len(dv) > 0:
            yearly = dv.groupby(dv.index.year).sum()
            yrs, vals_ = list(yearly.index), list(yearly.values)
            if yrs and int(yrs[-1]) >= datetime.now().year:
                yrs, vals_ = yrs[:-1], vals_[:-1]      # 진행 중인 해 제외
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
    # ★ 2026-09-13 수정 — 분기가 5개 미만이면 예전에는 가장 오래된 분기와
    #   비교해 놓고 그 값을 "1년 변화" 로 썼다. 4개면 9개월치, 2개면 3개월치를
    #   1년으로 읽는다. 소형주·국내 종목에서 흔하다.
    #   이제는 5개 미만이면 분기 계산을 포기하고 연간 추세로 넘긴다.
    dilution = None
    qs = _row(qb, ["Ordinary Shares Number", "Share Issued"])
    if qs is not None:
        try:
            ser = qs.dropna().sort_index()          # 오래된 → 최신
            v = [float(x) for x in ser.values][::-1]  # 최신 → 오래된
            if len(v) > 4 and v[4] > 0:             # 정확히 1년 전과 비교
                dilution = (v[0] / v[4] - 1) * 100
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
    # ★ 2026-09-13 수정 — 예전엔 각자 .iloc[-4:] 를 합산했다.
    #   현금흐름이 3분기치뿐이거나 한 분기 늦게 들어오면
    #   분자와 분모가 서로 다른 기간을 덮어 이익의 질(OCF/NI)이 틀렸다.
    #   같은 분기끼리만, 4개가 다 있을 때만 계산한다.
    ocf_s = ni_s = None
    if ocf_q is not None and ni_q is not None:
        common_q = sorted(set(ocf_q.index) & set(ni_q.index))[-4:]
        if len(common_q) == 4:
            ocf_s = float(sum(float(ocf_q[x]) for x in common_q))
            ni_s = float(sum(float(ni_q[x]) for x in common_q))

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
    #
    # ★ 2026-09-13 수정 — 야후의 debtToEquity 를 검증 없이 초기값으로 썼다.
    #   자기자본이 음수인 회사(자본잠식)는 이 값이 음수로 온다.
    #   아래 루프는 e_ > 0 일 때만 덮어쓰므로 음수가 그대로 남고,
    #   채점 기준이 전부 "작을수록 좋다" 여서 세 점수 모두 만점을 받았다.
    #     성장 부채비율 10/10 + 장기 부채 안전성 10/10 + 다모다란 10/10
    #   게다가 debt < 30 조건에 걸려 차입 자사주매입 감점까지 풀렸다.
    #   음수나 숫자가 아니면 아예 버린다. 모르는 게 틀린 것보다 낫다.
    debt_ratio = info.get("debtToEquity")
    if not (isinstance(debt_ratio, (int, float)) and debt_ratio >= 0):
        debt_ratio = None
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
    # ★ 2026-09-13 수정 두 가지
    #   (1) 현금은 분기에서, 부채는 연간에서 가져오는 일이 있었다.
    #       각각 따로 최신값을 찾았기 때문이다. 날짜가 다른 두 숫자를 빼면
    #       그 사이에 빌린 돈이 통째로 빠진다.
    #       이제는 같은 표 안에서 둘 다 있는 가장 최근 날짜를 고른다.
    #   (2) _row 는 먼저 맞는 키를 쓰므로 "Cash And Cash Equivalents" 가
    #       항상 이겼다. 반도체 회사는 단기투자에 현금을 많이 두는데
    #       그게 통째로 빠져 순현금이 크게 작게 나왔다.
    #       넓은 라벨(단기투자 포함)을 먼저 찾도록 순서를 바꿨다.
    CASH_KEYS = ["Cash Cash Equivalents And Short Term Investments",
                 "Cash And Cash Equivalents"]
    netcash = None
    try:
        for df_ in (qb, bs):
            cr = _row(df_, CASH_KEYS)
            dr = _row(df_, ["Total Debt"])
            if cr is None:
                continue
            if dr is None:
                # 부채 행이 없으면 부채 0 으로 본다(무차입 회사)
                ser = cr.dropna().sort_index()
                if len(ser):
                    netcash = float(ser.iloc[-1])
                    break
                continue
            common_n = set(cr.dropna().index) & set(dr.dropna().index)
            if not common_n:
                continue
            dtn = max(common_n)
            netcash = float(cr[dtn]) - float(dr[dtn])
            break
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
            # ★ 2026-09-13 — sort_index() 없이 tail(8) 을 했다.
            #   야후가 최신순으로 주면 "가장 오래된 8분기" 를 집는다.
            #   fetch 의 다른 모든 곳은 _row 가 정렬해 주는데 여기만 빠져 있었다.
            df = pd.DataFrame(h_).sort_index()
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
        "eq_negative": eq_negative,
        "cagr": cagr, "dil_annual": dil_annual, "dilution": dilution,
        "ocf": ocf_s, "ni": ni_s, "cover": cover, "netcash": netcash,
        "beats": beats,
        "qgrowth": qgrowth, "rnd": rnd, "dy": dy, "div_yrs": div_yrs,
        "roic": roic, "reinv_eff": reinv_eff,
        # R&D 자본화 조정 ROIC — 점수에는 안 들어간다 (참고·검증용)
        "roic_adj": roic_adj, "roic_adj_note": roic_adj_note,
        "rnd_asset": rnd_asset, "rnd_amort": rnd_amort, "rnd_life": rnd_life,
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

    # 값이 빠진 날은 먼저 버린다. 한 칸만 NaN 이어도 20일 평균이 통째로
    # NaN 이 되어 점수를 못 내게 되기 때문이다. (2026-09-13)
    h = h.dropna(subset=["Close", "Volume"])
    if len(h) < 60:
        return None

    c, v = h["Close"], h["Volume"].astype(float)
    ret = c.pct_change()
    v20 = v.rolling(20).mean()

    # ★ 2026-09-13 — 20일 평균거래량이 0 이거나 NaN 이면
    #   OBV 변화율의 분모가 무너져 점수 전체가 의미를 잃는다.
    #   거래정지·무거래 종목이 그렇다. 아예 계산하지 않는다.
    v20_last = float(v20.iloc[-1]) if len(v20) else float("nan")
    if not (v20_last > 0):
        return None

    # 1) 대량거래일: 최근 20일 중 거래량 > 20일평균 x2, 양봉/음봉 구분
    last20 = h.tail(20)
    spike = last20[v.tail(20) > (v20.tail(20) * 2)]
    # ★ 2026-09-13 — 시가가 비어 있으면 (종가 > 시가) 가 그냥 False 라
    #   양봉이 음봉으로 잘못 세어져 수급 점수가 근거 없이 깎였다.
    #   시가가 없는 날은 전일 종가로 대신한다 (통상 관행).
    op = h["Open"] if "Open" in h.columns else c.shift(1)
    op = op.fillna(c.shift(1)).fillna(c)
    spike_up = int((spike["Close"] > op.reindex(spike.index)).sum())
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
        """x 를 lo~hi 구간에서 0~1 로. 범위를 벗어나면 0 또는 1.

        ★ 2026-09-13 — NaN 방어가 없었다.
          파이썬에서 min(1.0, nan) 은 1.0 이라 NaN 이 그대로 만점이 됐다.
          거래량이 0 인 날이 하나만 있어도 obv_chg 가 NaN 이 되고,
          가중치 90 중 75 가 만점으로 채워져 수급 93점 "매수 우위" 가 나왔다.
          모르면 중립(0.5)으로 둔다.
        """
        if x != x or hi == lo:          # x != x 는 NaN 판정
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
        # ★ 2026-09-13 — 허용 폭이 절반(0.5)이라 126봉짜리가
        #   "1년 수익률" 로 표시됐다. 상장 6~11개월 종목에서 생긴다.
        #   ret_6m 과 ret_1y 가 똑같은 숫자로 나오면 그 경우다.
        #   0.9 로 좁혀 거의 1년치가 있을 때만 값을 준다.
        if len(c) < 2:
            return None
        idx = len(c) - 1 - days
        if idx < 0:
            if len(c) < days * 0.9:
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
# 시장 상황 (VIX · 원달러)          2026-09-13 추가
#
#   ★ 표시 전용이다. 채점에는 한 글자도 연결하지 않는다.
#
#   특히 환율은 절대 USD_KRW 를 대체하면 안 된다.
#   USD_KRW 는 mcap_krw 를 만들고, mcap_krw 는 score_ten 의
#   "시가총액" 15점에 1천억/5천억/2조/10조 라는 딱딱한 경계로 걸려 있다.
#   환율을 실시간으로 물리면 회사는 그대로인데 원화가 움직였다는 이유만으로
#   종목이 경계를 넘나들며 점수가 매일 달라진다.
#   6개월 순위 실험에서 그건 그냥 잡음이다. 실험 기간에는 고정이 맞다.
#   (실험이 끝나면 v2 에서 다시 볼 것)
# ═════════════════════════════════════════════════════════════

@cache(600)
def macro():
    """VIX 와 원달러 환율. 10분 캐시.

    각각 따로 try 로 감싼다. 하나가 실패해도 나머지는 보여 줘야 하고,
    둘 다 실패해도 앱은 멀쩡히 열려야 한다.
    """
    out = {"vix": None, "vix_chg": None, "usdkrw": None, "usdkrw_chg": None,
           "asof": None, "usdkrw_fixed": USD_KRW}

    def last_two(sym, period="10d"):
        h = yf.Ticker(sym).history(period=period)
        if h is None or len(h) < 1:
            return None, None, None
        c = h["Close"].dropna()
        if not len(c):
            return None, None, None
        cur = float(c.iloc[-1])
        prev = float(c.iloc[-2]) if len(c) >= 2 else None
        chg = ((cur / prev - 1) * 100) if prev and prev > 0 else None
        return cur, chg, str(c.index[-1])[:16]

    try:
        v, ch, ts = last_two("^VIX")
        out["vix"], out["vix_chg"] = v, ch
        out["asof"] = ts or out["asof"]
    except Exception:
        pass
    try:
        r, ch, ts = last_two("USDKRW=X")
        out["usdkrw"], out["usdkrw_chg"] = r, ch
        out["asof"] = ts or out["asof"]
    except Exception:
        pass
    return out


def vix_mood(v):
    """VIX 를 말로. 채점 아님, 읽기 도우미."""
    if v is None:
        return "-", "#9CA3AF"
    if v >= 30:
        return "공포", "#2563EB"
    if v >= 22:
        return "불안", "#5B7FC7"
    if v >= 17:
        return "보통", "#9CA3AF"
    if v >= 13:
        return "안정", AMBER
    return "과열 주의", ORANGE


# ═════════════════════════════════════════════════════════════
# 가치 평가          2026-09-13 추가
#
#   ★ 채점(score_ten / score_lt / score_damo)과 완전히 별개다.
#     SCORE_VERSION 에 영향이 없다. 어떤 채점 함수도 이 아래를 호출하지 않는다.
#
#   중심 생각:
#     "적정주가를 맞힌다" 는 애초에 되는 일이 아니다.
#     DCF 는 가치의 대부분이 10년 뒤 가정에서 나오고,
#     애널 목표가는 구조적으로 높게 잡히고,
#     PEG 적정가는 근거 없는 어림셈이다.
#
#     그래서 중심에 두는 것은 예측이 아니라 '사실 진술' 이다.
#       "지금 이 가격은 연 몇 % 성장을 요구하고 있는가"
#     이건 미래를 안 맞혀도 참이다. 판단은 사람이 한다.
#     적정가 세 개는 그 아래에 '범위' 로만 둔다.
# ═════════════════════════════════════════════════════════════

def implied_growth(d, target_peg=1.0):
    """지금 주가가 요구하는 연 성장률 (%).

    PEG = PER / 성장률 이므로, PEG 가 target 이 되는 성장률은
      성장률 = PER / target
    예측이 아니다. 지금 가격에 이미 박혀 있는 기대치를 되읽는 것뿐이다.
    """
    per = d.get("per")
    if not per or per <= 0 or target_peg <= 0:
        return None
    return per / target_peg


def peg_fair_price(d, target_peg=1.0):
    """PEG 가 target 이 되는 주가.

    피터 린치식 어림셈이다. 이론적 근거는 약하다.
    범위의 한쪽 끝으로만 쓴다.
    """
    per, g, px = d.get("per"), d.get("growth"), d.get("price")
    if not (per and g and px) or per <= 0 or g <= 0:
        return None
    return px * (g * target_peg) / per


def dcf_value(d, growth=None, margin=None, wacc=9.0, terminal=3.0,
              tax=21.0, years=10, reinvest=None):
    """다모다란식 간단 DCF. cli.py 의 dcf 명령과 같은 뼈대.

    ★ 통화 문제를 피하려고 '현재 시총 대비 배수' 로 계산한 뒤
      마지막에 현재가에 곱한다.
      매출은 재무통화(TWD 등), 주가는 상장통화(USD) 인 ADR 에서
      금액끼리 직접 비교하면 값이 수백 배로 틀어진다.
      배수로 가면 통화가 약분되어 그 사고가 원천적으로 안 난다.

    가정을 안 주면 회사 자기 숫자로 채운다:
      · 성장률  = 실제 매출 CAGR (단 10년 가정이므로 30% 로 자른다)
      · 이익률  = 현재 영업이익률
      · 재투자율 = 성장률 / ROIC   ← 다모다란의 핵심 연결고리
                  ("재투자 없는 성장은 없다")

    돌려주는 dict 의 note 에 무엇을 어떻게 가정했는지 전부 적는다.
    """
    px = d.get("price")
    revs = d.get("revs") or []
    mcap = d.get("mcap_fin") or d.get("mcap_raw")
    if not px or not revs or not mcap or mcap <= 0:
        return {"price": None, "note": "매출 또는 시가총액을 못 구함"}

    rev0 = float(revs[-1])
    if rev0 <= 0:
        return {"price": None, "note": "매출이 0 이하"}

    notes = []

    # ── 성장률 ──
    if growth is None:
        g_raw = d.get("growth")
        if g_raw is None:
            return {"price": None, "note": "매출 성장률을 못 구함"}
        growth = g_raw
        if growth > 30:
            notes.append(f"성장률 {g_raw:.0f}% → 30% 로 낮춰 잡음 "
                         f"(10년 내내 {g_raw:.0f}% 는 비현실적)")
            growth = 30.0
        if growth < 0:
            notes.append(f"성장률이 음수({g_raw:.0f}%) — 0% 로 잡음")
            growth = 0.0

    # ── 이익률 ──
    if margin is None:
        m_raw = d.get("margin")
        if m_raw is None:
            return {"price": None, "note": "영업이익률을 못 구함"}
        if m_raw <= 0:
            return {"price": None,
                    "note": f"영업이익률이 적자({m_raw:.0f}%) — "
                            f"목표 이익률을 사람이 정해야 계산됨"}
        margin = m_raw

    # ── 재투자율 = 성장률 / ROIC (다모다란) ──
    if reinvest is None:
        rc = d.get("roic")
        if rc and rc > 0:
            reinvest = min(90.0, max(0.0, growth / rc * 100))
            notes.append(f"재투자율 {reinvest:.0f}% = 성장률 {growth:.0f}% "
                         f"÷ ROIC {rc:.0f}%")
        else:
            reinvest = 40.0
            notes.append("ROIC 를 못 구해 재투자율 40% 로 가정")

    if wacc <= terminal:
        return {"price": None,
                "note": f"자본비용({wacc:.1f}%)이 영구성장률({terminal:.1f}%) 이하"}

    g, mgn = growth / 100, margin / 100
    tx, w, ri, gt = tax / 100, wacc / 100, reinvest / 100, terminal / 100

    rev, pv_sum, fcf_last = rev0, 0.0, 0.0
    for i in range(1, years + 1):
        rev *= (1 + g)
        fcf = rev * mgn * (1 - tx) * (1 - ri)
        pv_sum += fcf / ((1 + w) ** i)
        fcf_last = fcf

    tv = fcf_last * (1 + gt) / (w - gt)
    tv_pv = tv / ((1 + w) ** years)
    ev = pv_sum + tv_pv
    eq = ev + (d.get("netcash") or 0)

    if ev <= 0:
        return {"price": None, "note": "계산된 가치가 0 이하"}

    ratio = eq / mcap
    return {"price": px * ratio,
            "ratio": ratio,
            "tv_share": tv_pv / ev * 100,
            "growth": growth, "margin": margin, "wacc": wacc,
            "terminal": terminal, "reinvest": reinvest, "years": years,
            "note": " · ".join(notes) if notes else None}


def fair_range(d, wacc=9.0, target_peg=1.0):
    """적정가 세 가지를 모아 범위로.

    하나를 믿는 게 아니라 '얼마나 벌어져 있나' 를 보는 게 목적이다.
    세 값이 크게 엇갈리면 그 자체가 "값을 매기기 어려운 회사" 라는 신호다.
    """
    px = d.get("price")
    out = {"price": px, "items": [], "lo": None, "hi": None,
           "spread": None, "pos": None}
    if not px:
        return out

    dcf = dcf_value(d, wacc=wacc)
    if dcf.get("price") and dcf["price"] > 0:
        out["items"].append(("DCF", dcf["price"], dcf))
    else:
        out["dcf_fail"] = dcf.get("note")

    pf = peg_fair_price(d, target_peg)
    if pf and pf > 0:
        out["items"].append(("PEG", pf, None))

    tg = d.get("tgt")
    if tg and tg > 0:
        out["items"].append(("애널", float(tg), None))

    vals = [v for _, v, _ in out["items"]]
    if vals:
        out["lo"], out["hi"] = min(vals), max(vals)
        # 최저 대비 최고가 몇 배인가 — 3배 넘게 벌어지면 경고할 값이다
        out["spread"] = out["hi"] / out["lo"] if out["lo"] > 0 else None
        if out["hi"] > out["lo"]:
            out["pos"] = (px - out["lo"]) / (out["hi"] - out["lo"]) * 100
        else:
            out["pos"] = 50.0
    return out


# ═════════════════════════════════════════════════════════════
# 성장 가속도 (연간)   ★ 채점과 완전히 별개 — 2026-09-13 추가
# ═════════════════════════════════════════════════════════════
#
#   ★★ score_ten / score_lt / score_damo 어디서도 이 함수를 부르지 않는다.
#      SCORE_VERSION 1 은 2027-03 까지 고정이다. 지금은 기록만 하고,
#      6개월 뒤 "이 값이 순서를 매겼나" 를 돈 안 걸고 계산하려고 만든 것이다.
#      (damo / roic_adj 를 넣은 것과 같은 이유)
#
#   ■ 왜 만들었나
#      채점표에 '성장 가속도 20점' 이 있는데 한 번도 켜진 적이 없다.
#      분기 매출 YoY 를 3개 비교하려면 분기 매출이 7개 필요한데
#      야후 무료는 4~5개만 준다. 설계에는 있고 데이터가 없는 항목이다.
#
#      연간 매출은 4~5년치가 온다. 같은 질문을 연 단위로 물으면
#      YoY 를 3~4개 만들 수 있다. 분기보다 둔하지만 계산은 된다.
#
#   ■ 무엇을 잡으려는 것인가
#      "매출이 크다" 가 아니라 "매출 성장률이 빨라지고 있다".
#      팔란티어 2021~2025
#        매출  1.54 → 1.91 → 2.23 → 2.87 → 4.48 (십억 달러)
#        YoY          23.6   16.8   28.8   56.2 (%)
#        끝에서부터 커진 횟수 = 2 (16.8 → 28.8 → 56.2)
#
#   ■ 한계 (쓰기 전에 알 것)
#      · 연 단위라 반응이 1년 이상 늦다
#      · 가속하다 꺾이는 회사가 끝까지 가는 회사보다 훨씬 많다
#      · 가속이 눈에 보이는 시점에는 이미 비싸다
#        (팔란티어가 PSR 74배다. 발견과 수익은 다른 문제다)
#      · 기저효과로 켜진다. 재작년에 크게 빠진 회사는 그냥 회복인데
#        가속으로 잡힌다. 그래서 매출 절대액을 같이 봐야 한다.
#      → 그래서 점수가 아니라 기록이다. 6개월 뒤에 판정한다.
# ═════════════════════════════════════════════════════════════

def growth_accel(revs):
    """연간 매출 성장률이 연속으로 빨라졌는지.

    revs 는 오래된 것이 앞이다 (_row 가 sort_index 하므로).
    반환
        yoy    연간 성장률 목록 (오래된 → 최신, %)
        n_up   끝에서부터 연속으로 커진 횟수
        n_max  비교 가능한 최대 횟수 (= len(yoy) - 1)
        last   가장 최근 성장률
        pp     최근 − 직전 (%p). 가속의 '세기'
        verdict
    """
    if not revs or len(revs) < 3:
        return None
    try:
        rv = [float(x) for x in revs]
    except Exception:
        return None

    yoy = []
    for a, b in zip(rv, rv[1:]):
        if a and a > 0:
            yoy.append((b / a - 1) * 100)
        else:
            return None            # 매출 0/음수 — 계산이 의미 없다
    if len(yoy) < 2:
        return None

    # ★ 2026-09-13 — 처음엔 yoy[i] > yoy[i-1] 로 썼다가 바로 고쳤다.
    #   매출 100 → 120 → 144 → 172.8 은 매년 정확히 20% 인데
    #   부동소수점 때문에 20.000000000000018 > 20.0 이 참이 되어
    #   "가속 2/3" 이 나왔다. 한 푼도 안 빨라진 회사가 가속으로 잡힌 것이다.
    #   그리고 0.1%p 빨라진 것을 가속이라 부를 이유도 없다.
    #   1%p 를 넘어야 한 칸으로 센다.
    EPS_PP = 1.0
    n_up = 0
    for i in range(len(yoy) - 1, 0, -1):
        if yoy[i] - yoy[i - 1] > EPS_PP:
            n_up += 1
        else:
            break

    last, prev = yoy[-1], yoy[-2]
    pp = last - prev

    # ★ 매출이 줄고 있으면 '가속' 이라고 부르지 않는다.
    #   -40% 에서 -10% 로 온 것도 수식으로는 상승이지만
    #   그건 가속이 아니라 '덜 나빠짐' 이다. 섞으면 안 된다.
    if last < 0:
        verdict = "매출 감소"
    elif n_up >= 3:
        verdict = "강한 가속"
    elif n_up == 2:
        verdict = "가속"
    elif n_up == 1:
        verdict = "개선"
    else:
        verdict = "둔화"

    # 한 해라도 역성장이 섞였으면 기저효과일 수 있다.
    #   재작년에 크게 빠진 회사는 그냥 회복인데 가속으로 잡힌다.
    #   예: 100 → 60 → 70 → 95 → 140 은 3/3 강한 가속으로 나오지만
    #   5년 전 매출을 이제 겨우 넘긴 회사다.
    low_base = min(yoy) < 0

    return {"yoy": [round(x, 1) for x in yoy],
            "n_up": n_up, "n_max": len(yoy) - 1,
            "last": round(last, 1), "pp": round(pp, 1),
            "low_base": low_base,
            "verdict": verdict}


def accel_text(a):
    """한 줄 표기.  예) '가속 2/3 · 최근 +56% (+27%p)'"""
    if not a:
        return "-"
    # 매출이 줄고 있으면 연속 횟수를 안 보여 준다.
    # "매출 감소 3/3" 은 가속처럼 읽힌다. 그건 덜 나빠지는 중일 뿐이다.
    head = (a["verdict"] if a["verdict"] == "매출 감소"
            else f"{a['verdict']} {a['n_up']}/{a['n_max']}")
    # ※기저 는 "가속처럼 보이지만 회복일 수 있다" 는 경고다.
    # 둔화·매출 감소에는 경고할 가속 주장이 없으므로 안 붙인다. (2026-09-13)
    tail = " ※기저" if a.get("low_base") and a["n_up"] >= 1 and \
        a["verdict"] != "매출 감소" else ""
    return f"{head} · 최근 {a['last']:+.0f}% ({a['pp']:+.0f}%p){tail}"


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
