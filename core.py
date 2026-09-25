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
#   2  2026-09-17  성장 135점 / 14항목 으로 축소.
#                  · 어닝 서프라이즈 15점 제거 — 관측 8,321건으로
#                    예측력 없음 확인 (RETIRED 로 이동)
#                  · 성장 가속도 20점 제거 — 야후 분기가 4~5개뿐이라
#                    채점이 구조적으로 불가 (RETIRED 로 이동)
#                  · "이익률 추세" 를 (분기)/(연간) 으로 분리
#                    같은 이름으로 서로 다른 기간을 재고 있었다
#                  · "하락 회복력" → "고점 대비 위치" 로 개명
#                    회복력이 아니라 고점 대비 낙폭을 재는 항목이었다
#                  ★ v1 으로 쌓은 history 는 지문이 달라 verify 에서
#                    자동으로 빠진다. 실험 기준점을 다시 찍어야 한다.
# ═════════════════════════════════════════════════════════════
# 파일이 언제 만들어진 것인지. 옛 파일을 쓰고 있는지 바로 알려고 둔다.
BUILD = "2026-09-25 07:19"

SCORE_VERSION = 2

USD_KRW = 1380

# ── 색상 (app.py 와 공용) ────────────────────────────────────
#   밝은 배경 기준이다. Streamlit 설정 → Theme 를 Light 로 둘 것.
#   Dark 로 두면 진한 글씨가 배경에 묻혀 안 보인다.
CHARCOAL = "#2F3437"
ORANGE = "#EA580C"
AMBER = "#B45309"
SLATE = "#3F4956"      # 중하위
MUTED = "#4A5462"      # 하위권 · 설명 글씨
BLUE = "#2563EB"
GRAY = MUTED
억 = 1_0000_0000
조 = 1_0000_0000_0000

CACHE_DIR = ".cache"


# ─────────────────────────────────────────────────────────────
# 캐시: streamlit 있으면 st.cache_data, 없으면 파일 캐시
# ─────────────────────────────────────────────────────────────

def _is_empty(r):
    """캐시하면 안 되는 '실패' 결과인가."""
    if r is None:
        return True
    if isinstance(r, (list, dict, tuple, str)) and len(r) == 0:
        return True
    return False


def _file_cache(ttl):
    """CLI 전용 디스크 캐시 데코레이터."""
    def deco(fn):
        # 함수 코드가 바뀌면 캐시 이름도 바뀌게 한다 → 코드를 고치면 새로 받는다.
        #   (전에는 매번 .cache 를 손으로 지워야 했다)
        try:
            import hashlib, inspect as _ins
            ver = hashlib.md5(_ins.getsource(fn).encode("utf-8")).hexdigest()[:6]
        except Exception:
            ver = "0"

        def wrap(*a, **kw):
            # 키워드 인자도 받는다. 안 받으면 filings(t, kinds=[...]) 가 터진다.
            os.makedirs(CACHE_DIR, exist_ok=True)
            parts = list(map(str, a)) + [f"{k}={v}" for k, v in sorted(kw.items())]
            key = f"{fn.__name__}_{ver}_{'_'.join(parts)}".replace("/", "_")
            p = os.path.join(CACHE_DIR, key + ".json")
            if os.path.exists(p) and time.time() - os.path.getmtime(p) < ttl:
                try:
                    with open(p, encoding="utf-8") as f:
                        v = json.load(f)
                    if not _is_empty(v):        # 예전에 저장된 빈 값은 버린다
                        return v
                except Exception:
                    pass
            r = fn(*a, **kw)
            # ★ 실패(빈 값)는 저장하지 않는다.
            #   야후가 잠깐 막혀 None 을 준 것을 저장하면
            #   풀린 뒤에도 TTL 동안 계속 "찾을 수 없음" 이 된다.
            if _is_empty(r):
                return r
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

    class _NoCache(Exception):
        """빈 결과를 캐시에 남기지 않으려고 쓰는 예외."""
        def __init__(self, v):
            self.v = v

    def cache(ttl):
        # ★ st.cache_data 는 None 도 그대로 저장한다.
        #   야후가 잠깐 막혔을 때의 None 이 15분 동안 남아
        #   앱에서 "데이터를 찾을 수 없습니다" 가 계속 떴다.
        #   예외는 캐시하지 않으므로, 빈 값이면 예외로 빼낸다.
        def deco(fn):
            def inner(*a, **kw):
                r = fn(*a, **kw)
                if _is_empty(r):
                    raise _NoCache(r)
                return r
            # ★ 스트림릿은 '함수 코드' 로 캐시를 구분한다. 포장지(inner) 코드만 보이면
            #   진짜 함수를 고쳐도 같은 함수로 알고 예전 결과를 계속 쓴다.
            #   (2026-09-24: 개요·대주주가 안 나온 원인) → 진짜 함수 코드가 보이게 한다.
            import functools
            functools.update_wrapper(inner, fn)
            cached = st.cache_data(ttl=ttl, show_spinner=False)(inner)

            def outer(*a, **kw):
                try:
                    return cached(*a, **kw)
                except _NoCache as e:
                    return e.v
            outer.__name__ = fn.__name__
            outer.clear = getattr(cached, "clear", lambda: None)
            return outer
        return deco
else:                            # CLI 단독 실행 → 파일 캐시
    cache = _file_cache


# ═════════════════════════════════════════════════════════════
# 채점 로직
# ═════════════════════════════════════════════════════════════

def _row(df, keys):
    """재무제표에서 한 줄을 꺼낸다. 후보 이름을 차례로 찾는다.

    야후는 회사마다 행 이름이 달라서 후보를 여러 개 준다.
    (ASML 은 "Operating Cash Flow" 가 없어 항목이 통째로 빠졌었다)

    ★ 날짜순으로 정렬해서 돌려준다. iloc[-1] 이 항상 최신이다.
      야후 분기 컬럼은 최신순으로 오는 경우가 있어, 정렬하지 않으면
      가장 오래된 값을 최신으로 착각한다.
      주식수·부채비율·순현금·4분기합에서 같은 버그를 네 번 겪었다.
    """
    if df is None or df.empty:
        return None
    for k in keys:
        if k in df.index:
            s = df.loc[k].dropna()
            if len(s):
                return s.sort_index()
    return None


def _at(row_, key):
    """그 날짜의 값. 없거나 NaN 이면 None.

    없는 값을 0 으로 채우면 "모르는 회사" 가 "나쁜 회사" 가 된다.
    부르는 쪽에서 None 인지 보고 판단하게 한다.
    """
    if row_ is None or key not in row_.index:
        return None
    v = row_[key]
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    try:
        return float(v)
    except Exception:
        return None


def _last(df, keys):
    """가장 최근 값 하나. 컬럼 순서가 뒤바뀌어 와도 안전하다."""
    r = _row(df, keys)
    if r is None:
        return None
    s = r.dropna()
    return float(s.iloc[-1]) if len(s) else None


def _sum_last(row_, n=4):
    """최근 n 개 합. 야후 분기 컬럼이 최신순으로 오는 경우가 있어
    _row 가 정렬해 둔 것을 전제로 뒤에서 n 개를 더한다."""
    if row_ is None:
        return None
    s = row_.dropna()
    return float(s.iloc[-n:].sum()) if len(s) else None


# ── 쉬는 항목 ────────────────────────────────────────────────
#   배점표에서 빼되 계산 코드는 남겨 둔다.
#   근거가 생기면 이 목록에서 그 줄만 지우면 되살아난다.
RETIRED = {
    "어닝 서프라이즈":
        "관측 8,321건으로 예측력 없음 확인 (2026-09, EDGAR 대조)",
    "성장 가속도":
        "야후 분기 재무제표가 4~5개뿐이라 채점 불가. "
        "389종목 전부에서 한 번도 안 나왔다. EDGAR 를 붙이면 되살릴 수 있다",
}

TEN_MAX = {"시가총액": 15, "영업이익률": 10, "부채비율": 10, "내부자지분": 10,
           "ROE": 5, "밸류에이션": 10, "이익률 추세(분기)": 15,
           "실적-주가 괴리": 10, "순현금/시총": 10,
           "주식수 희석": 10, "이익의 질": 10, "잉여현금흐름": 10,
           "R&D 집중도": 5, "고점 대비 위치": 5}

LT_MAX = {"FCF 안정성": 20, "이익률 안정성": 15, "자본수익률": 15,
          "침체 생존력": 15, "장기 성장률": 15, "주식수 관리": 10,
          "배당": 10, "부채 안전성": 10, "밸류에이션": 10}

# ── 다모다란 관점 점수 (100점) ──
#   기존 두 채점과는 별개다. SCORE_VERSION 에 영향을 주지 않는다.
#   "지금 재무가 좋은가" 가 아니라
#   "자본을 굴려 가치를 만들고 있는가" 를 본다.
#   앞 두 항목에 60점을 몰았다. 다모다란이 가장 중요하게 보는 것이라서.
DAMO_MAX = {"ROIC 초과수익": 35, "재투자 효율": 25, "이익률 추세(연간)": 20,
            "부채 안전성": 10, "함정 없음": 10}
DAMO_WACC = 9.0          # 자본비용 기본 가정 %



def _numify(d, keys):
    """숫자여야 하는 값을 숫자로 바꾼다. 못 바꾸면 None.

    ★ 야후가 숫자 자리에 글자를 주는 종목이 있다.
      옛 snapshot.json 을 읽을 때도 같은 일이 생긴다.
      채점 함수 앞에서 한 번 걸러 둔다.
    """
    import math
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            # NaN·무한대는 '값 있음' 으로 잘못 읽힌다 (bool(nan) 은 True) → 빈칸
            if isinstance(v, float) and not math.isfinite(v):
                d[k] = None
            continue
        try:
            f = float(str(v).replace(",", "").strip())
            d[k] = f if math.isfinite(f) else None
        except Exception:
            d[k] = None
    return d


def _numify_lists(d, keys):
    """목록 안의 값도 숫자로. 못 바꾸는 값은 뺀다 (sum·min 이 글자에서 터지지 않게)."""
    for k in keys:
        v = d.get(k)
        if not isinstance(v, (list, tuple)):
            continue
        import math
        out = []
        for x in v:
            if isinstance(x, (int, float)):
                if not (isinstance(x, float) and not math.isfinite(x)):
                    out.append(x)
                continue
            try:
                f = float(str(x).replace(",", "").strip())
                if math.isfinite(f):
                    out.append(f)
            except Exception:
                pass
        d[k] = out
    return d


_SCORE_LISTKEYS = ("revs", "margins", "roes", "fcfs", "qgrowth", "qmargin",
                   "shares_hist", "y_growth", "y_margin", "y_debt", "y_roic", "y_fcfm")


def _clean_inputs(fn):
    """점수 함수 앞에서 입력을 청소하는 겉싸개.

    ★ 점수 함수 본문을 고치면 채점 지문이 바뀌어 verify 기록이 갈린다.
      그래서 본문은 그대로 두고 바깥에서 감싼다. functools.wraps 로 감싸면
      inspect.getsource 가 원래 본문을 보므로 지문도 그대로다.
    """
    import functools

    @functools.wraps(fn)
    def w(d, *a, **k):
        if isinstance(d, dict):
            _numify(d, _SCORE_NUMKEYS)
            _numify_lists(d, _SCORE_LISTKEYS)
        return fn(d, *a, **k)
    return w


# 채점 함수가 숫자로 쓰는 키 전부.
#   debt_asof · roic_note · reinv_note · netcash_note 는 원래 글자라 뺀다.
_SCORE_NUMKEYS = ("mcap_krw", "margin", "debt", "insider", "roe", "per",
                  "peg", "growth", "cagr", "rev1y", "px1y", "dd", "rnd",
                  "dilution", "dil_annual", "netcash", "mcap_fin", "ocf",
                  "ni", "dy", "div_yrs", "roic", "reinv_eff", "fcf",
                  "debt_yf", "inst", "tgt", "short_pct",
                  "bvps_chg", "capex_chg", "capex_last", "cover",
                  "debt_chg", "fcf_last", "ocf_last")


def score_ten(d):
    _numify(d, _SCORE_NUMKEYS)
    o = []
    m = d["mcap_krw"]
    if m:
        # 구간만 보여 주면 실제 규모를 알 수 없어 값도 같이 적는다
        band = ("1천억↓" if m < 1000*억 else "1천~5천억" if m < 5000*억
                else "5천억~2조" if m < 2*조 else "2~10조" if m < 10*조
                else "10조↑")
        o.append(("시가총액", 15 if m < 1000*억 else 13 if m < 5000*억
                  else 8 if m < 2*조 else 4 if m < 10*조 else 0,
                  f"{won(m)} ({band})"))
    # ★ 야후의 손익·현금흐름은 TTM(최근 4분기)이다. 회계연도가 아니다.
    #   ALAB 에서 확인: 야후 22.8% vs 공시 FY2025 20.3%
    #   어느 기간인지 안 적으면 회계연도인 줄 알고 읽게 된다.
    v = d["margin"]
    if v is not None:
        o.append(("영업이익률", 10 if v >= 25 else 8 if v >= 15 else 4 if v >= 5
                  else 2 if v >= 0 else 0, f"{v:.1f}% (TTM)"))
    v = d["debt"]
    if v is not None:
        asof = f" ({d['debt_asof']})" if d.get("debt_asof") else ""
        # 부채비율이 음수면 자기자본이 마이너스라는 뜻이다(자본잠식).
        # 최악의 상태인데 "50% 미만" 으로 읽혀 만점을 받고 있었다.
        if v < 0:
            o.append(("부채비율", 0, f"{v:.0f}% — 자본잠식 (자기자본 마이너스)"))
        else:
            o.append(("부채비율",
                      10 if v < 50 else 8 if v < 100 else 4 if v < 200 else 0,
                      f"{v:.0f}%{asof}"))
    v = d["insider"]
    if v is not None:
        o.append(("내부자지분", 10 if v >= 30 else 8 if v >= 15 else 4 if v >= 5 else 1,
                  f"{v:.1f}%"))
    v = d["roe"]
    if v is not None:
        o.append(("ROE", 5 if v >= 20 else 4 if v >= 12 else 2 if v >= 5 else 0,
                  f"{v:.1f}% (TTM)"))
    peg, per, g = d["peg"], d["per"], d["growth"]

    # PEG 가 지나치게 낮은 것은 대개 적자→흑자 전환 때
    # 성장률이 수천 % 로 튀어서 생긴 값이다.
    #   AEHR PEG 0.02 (PER 43.7), ALAB PEG 0.02 (PER 141)
    # 일회성 기저효과를 "싸다" 로 읽으면 안 되므로
    # PEG 0.2 미만이면 믿지 않고 PER 절대수준으로 평가한다.
    # ★ 0.2 와 80% 는 근거 없이 정한 경계다. 검증되지 않았다.
    #   적자→흑자 전환 때 PEG 가 0 에 가까워지는 것을 걸러내려는 것인데,
    #   어디서 끊어야 맞는지는 모른다.
    peg_ok = bool(peg) and peg > 0.2
    # PEG 를 믿을 수 없으면 같은 이익에서 나온 성장률도 믿지 않는다.
    # (ALAB 은 PEG 0.02 를 걸러도 성장률 120% 로 다시 후한 점수를 받았다)
    g_ok = (bool(g) and 0 < g <= 80
            and not (bool(peg) and peg <= 0.2))

    if peg_ok:
        o.append(("밸류에이션", 10 if peg < 1 else 8 if peg < 1.5 else 5 if peg < 2.5
                  else 2 if peg < 4 else 0, f"PEG {peg:.2f}"))
    elif per and per > 0 and g_ok:
        # g 는 d["growth"] = 연평균 성장률(CAGR)이다. 작년 성장률이 아니다.
        # 그냥 "성장" 이라고 쓰면 최근 실적과 안 맞아 보인다.
        r = per / g
        o.append(("밸류에이션", 10 if r < 1 else 8 if r < 1.5 else 5 if r < 2.5 else 2,
                  f"PER {per:.0f} (연평균 성장 {g:.0f}%)"))
    elif per and per > 0 and (peg or g):
        # 성장률이나 PEG 는 있는데 믿을 수 없는 값인 경우.
        # PER 절대수준으로만 보고, 그 사실을 화면에 밝힌다.
        # 왜 PEG·성장률을 안 쓰는지 이유를 정확히 적는다.
        #   음수  → 적자거나 역성장. 기저효과가 아니다.
        #   0.2 미만 → 적자에서 흑자로 돌아설 때 생기는 착시
        if peg is not None and peg < 0:
            why = f"PEG {peg:.2f} 적자·역성장이라"
        elif peg is not None and peg <= 0.2:
            why = f"PEG {peg:.2f} 기저효과(0.2↓)라"
        elif g is not None and g < 0:
            why = f"연평균 성장 {g:.0f}% 역성장이라"
        else:
            why = f"연평균 성장 {g:.0f}% 기저효과(80%↑)라"
        o.append(("밸류에이션",
                  9 if per < 10 else 7 if per < 15 else 5 if per < 25
                  else 3 if per < 40 else 1 if per < 60 else 0,
                  f"PER {per:.1f} ({why} 제외 · 이 10점을 좌우하는 경계 0.2/80%는 임의)"))
    elif per and per > 0:
        # 성장률을 못 구한 경우(역성장 포함) PER 절대수준으로만 평가.
        # 성장 대비 평가가 아니므로 만점은 주지 않는다.
        o.append(("밸류에이션", 9 if per < 10 else 7 if per < 15 else 5 if per < 25
                  else 3 if per < 40 else 1 if per < 60 else 0,
                  f"PER {per:.1f} (성장률 미확인)"))
    # ★ 성장 가속도(20점)는 사실상 채점되지 않는다.
    #   분기 매출 YoY 를 3개 만들려면 분기 재무제표가 7개 필요한데
    #   야후는 보통 4~5개만 준다.
    #   그래서 만점이 조용히 170 → 150 이 된다.
    #   항목이 빠진 걸 모르고 지나치지 않도록 화면에 이유를 남긴다.
    seq = d.get("qgrowth")
    if "성장 가속도" not in RETIRED and isinstance(seq, (list, tuple)) and len(seq) >= 3:
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
    q = d.get("qmargin")
    if isinstance(q, (list, tuple)) and len(q) >= 3:
        a = sum(q[:2]) / 2
        b = sum(q[2:4]) / len(q[2:4]) if len(q) >= 4 else q[2]
        dd = a - b
        t = " → ".join(f"{x:.0f}" for x in reversed(q[:4]))
        # ★ 다모다란 쪽 "이익률 추세(연간)" 과 다른 것을 잰다.
        #   여긴 최근 4분기, 저긴 최근 4년. 같은 회사가 3/15 와 20/20 을
        #   동시에 받을 수 있다. 이름을 갈라 두었다.
        o.append(("이익률 추세(분기)",
                  15 if dd >= 5 else 12 if dd >= 1.5 else 7 if dd >= -1.5
                  else 3 if dd >= -5 else 0,
                  f"[{t}%] 최근 4분기"))
    beats = d.get("beats")
    bt, tt = beats if isinstance(beats, (list, tuple)) and len(beats) == 2 else (None, None)
    if tt and "어닝 서프라이즈" not in RETIRED:
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
        _nc_note = d.get("netcash_note")
        o.append(("순현금/시총", 10 if r >= 30 else 8 if r >= 10 else 5 if r >= 0
                  else 2 if r >= -20 else 0,
                  f"{r:+.0f}%" + (f"  ※{_nc_note}" if _nc_note else "")))
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
        o.append(("주식수 희석", base,
                  f"{v:+.1f}%{note} · 최근 4분기 대비"))
    ocf, ni = d["ocf"], d["ni"]
    if ocf is not None and ni:
        if ni < 0:
            o.append(("이익의 질", 4 if ocf > 0 else 0,
                      "순손실, 영업현금 흑자" if ocf > 0 else "순손실, 영업현금 적자"))
        else:
            r = ocf / ni
            o.append(("이익의 질", 10 if r >= 1.3 else 8 if r >= 1 else 5 if r >= .7
                      else 2 if r >= .3 else 0, f"OCF/NI {r:.2f} (최근 4분기)"))
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
        # ★ 이 항목은 "회복력" 이 아니다. 고점에서 얼마나 눌렸는지를 잰다.
        #   25~45% 빠진 자리에 만점을 준다 — 싸게 살 자리라고 본 것이다.
        #   근거 없는 구간이다. 화면에 무엇을 재는지 적는다.
        pos = ("고점 근처" if dd_ >= -10 else "얕은 눌림" if dd_ >= -25
               else "깊은 눌림" if dd_ >= -45 else "크게 빠짐" if dd_ >= -65
               else "반토막 이하")
        o.append(("고점 대비 위치",
                  2 if dd_ >= -10 else 4 if dd_ >= -25 else 5 if dd_ >= -45
                  else 3 if dd_ >= -65 else 1,
                  f"{dd_:.0f}% · {pos} (52주 고점 대비. 눌린 자리에 점수가 높다)"))
    return o


def score_lt(d):
    _numify(d, _SCORE_NUMKEYS)
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
        # 매출이 0 인 해가 있으면 나눌 수 없다. 그 구간은 건너뛴다.
        chg = [(b/a-1)*100 for a, b in zip(rv, rv[1:]) if a and a > 0]
        worst = min(chg) if chg else None
        if worst is None:
            o.append(("침체 생존력", 15 if drops == 0 else 6,
                      f"역성장 {drops}회"))
        else:
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
        # 야후가 주는 연간 주식수 구간의 연평균이다.
        # 상장 전 구간은 애초에 안 들어온다. 기간을 적어 둔다.
        o.append(("주식수 관리", base,
                  f"{v:+.1f}%/년{note} · 야후 연간 구간 평균"))
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
        if de < 0:                            # 자기자본 마이너스 = 자본잠식
            o.append(("부채 안전성", 0, t + " · 자본잠식"))
        elif cov is not None and cov <= 0:    # 영업적자 = 이자를 못 갚는 상태
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
        # ★ 자본비용 9% 는 임의값이다. 이 35점이 거기 달려 있다.
        # 화면에는 점수를 안 내므로 "몇 점" 얘기는 빼고
        # 자본비용이 임의값이라는 사실만 적는다.
        txt = (f"{rc:.1f}% (자본비용 {w:.0f}% 대비 {gap:+.1f}%p"
               f" · 자본비용 9%는 임의값)")
        # 가정을 썼거나 데이터가 빠졌으면 숨기지 않고 같이 적는다
        if d.get("roic_note"):
            txt += f"  ※{d['roic_note']}"
        o.append(("ROIC 초과수익", sc, txt))

    # ② 돈 1 을 써서 매출이 얼마나 늘었나 (25점)
    re_ = d.get("reinv_eff")
    if re_ is not None:
        sc = (25 if re_ >= 2.0 else 20 if re_ >= 1.5 else 15 if re_ >= 1.0
              else 8 if re_ >= 0.5 else 0)
        note = ("효율 높음" if re_ >= 2 else "보통" if re_ >= 0.5
                else "투자 회수 전" if re_ >= 0 else "매출 감소 중")
        txt = f"{re_:.2f}배 · {note}"
        if d.get("reinv_note"):
            txt += f"  ※{d['reinv_note']}"
        o.append(("재투자 효율", sc, txt))

    # ③ 이익률이 개선되는 방향인가 (20점)
    #    적자여도 방향이 맞으면 점수를 준다. 초기 성장주를 보기 위함.
    m = d.get("margins")
    if m and len(m) >= 3:
        late = sum(m[-2:]) / 2
        early = sum(m[:2]) / 2 if len(m) >= 4 else m[0]
        tr = late - early
        sc = (20 if tr >= 10 else 15 if tr >= 3 else 8 if tr >= -3 else 0)
        note = ("개선 중" if tr >= 3 else "횡보" if tr >= -3 else "악화 중")
        o.append(("이익률 추세(연간)", sc,
                  f"[{' → '.join(f'{x:.0f}' for x in m[-4:])}%] {tr:+.0f}%p · {note}"
                  " · 최근 4년"))

    # ④ 망하지 않을 여력 (10점)
    de = d.get("debt")
    if de is not None:
        if de < 0:
            o.append(("부채 안전성", 0,
                      f"부채비율 {de:.0f}% — 자본잠식"))
        else:
            sc = 10 if de <= 30 else 7 if de <= 60 else 4 if de <= 100 else 0
            o.append(("부채 안전성", sc, f"부채비율 {de:.0f}%"))

    # ⑤ 숫자가 튀어 좋아 보이는 함정 (10점)
    #    PEG 0.2 미만 = 적자→흑자 전환 착시
    #    ROE 100% 초과 = 자기자본이 쪼그라든 판정
    peg, roe = d.get("peg"), d.get("roe")
    traps = []
    if peg is not None and 0 < peg < 0.2:
        traps.append(f"PEG {peg:.2f}")
    if roe is not None and roe > 100:
        traps.append(f"ROE {roe:.0f}%")
    if peg is not None or roe is not None:
        sc = 10 if not traps else 5 if len(traps) == 1 else 0
        o.append(("함정 없음", sc,
                  ("없음 (PEG 0.2 미만 · ROE 100% 초과를 검사)"
                   if not traps else " / ".join(traps) + " 주의")))

    return o



# ═════════════════════════════════════════════════════════════
# 역산 (implied growth)
#
#   보통의 DCF 는 "성장률을 가정해서 적정가를 구한다".
#   역산은 반대다. 주가는 이미 정해져 있으니
#   "그 주가가 되려면 성장률이 얼마여야 하나" 를 거꾸로 푼다.
#
#   왜 이쪽이 나은가
#     · 가정을 내가 만들지 않아도 된다 (제일 틀리기 쉬운 부분)
#     · 나온 숫자를 실제 실적과 바로 대조할 수 있다
#     · "시장이 무엇을 믿고 있는가" 가 드러난다
#
#   다모다란이 implied growth 라 부르는 것이다.
# ═════════════════════════════════════════════════════════════


# 입력 청소 겉싸개 (본문·지문은 그대로)
score_ten = _clean_inputs(score_ten)
score_lt = _clean_inputs(score_lt)
score_damo = _clean_inputs(score_damo)

def dcf_value(rev0, growth, margin, reinvest, tax, wacc, terminal, years=10):
    """가정을 넣어 기업가치를 계산한다. (내부용)

    매출 → 영업이익 → 세후이익 → 재투자 차감 → 할인
    돌려주는 값은 영업가치(EV)다. 순현금은 부르는 쪽에서 더한다.
    """
    if wacc <= terminal:
        return None
    rev, pv = rev0, 0.0
    fcf = 0.0
    for i in range(1, years + 1):
        rev *= (1 + growth)
        nopat = rev * margin * (1 - tax)
        fcf = nopat * (1 - reinvest)
        pv += fcf / ((1 + wacc) ** i)
    tv = fcf * (1 + terminal) / (wacc - terminal)
    return pv + tv / ((1 + wacc) ** years)


def implied_growth(d, margin=None, reinvest=0.40, tax=0.21,
                   wacc=0.09, terminal=0.03, years=10):
    """지금 주가를 정당화하는 매출 성장률을 이분법으로 찾는다.

    돌려주는 값
      g        찾아낸 연평균 성장률 (소수. 0.28 이면 28%)
      margin   계산에 쓴 목표 영업이익률
      note     못 찾았을 때 이유
    """
    revs = d.get("revs") or []
    mcap = d.get("mcap_fin") or d.get("mcap_raw")
    if not revs or not mcap:
        return None, None, "매출 또는 시가총액 없음"

    rev0 = float(revs[-1])
    if rev0 <= 0:
        return None, None, "매출이 0 이하"

    # 목표 이익률을 안 주면 최근 실적에서 가져온다.
    # 최근 2년 평균을 쓴다. 한 해만 보면 일회성에 휘둘린다.
    if margin is None:
        m = d.get("margins") or []
        if len(m) >= 2:
            margin = sum(m[-2:]) / 2 / 100
        elif d.get("margin") is not None:
            margin = d["margin"] / 100
        else:
            return None, None, "영업이익률 없음"
    if margin <= 0:
        return None, None, "영업이익률이 0 이하 (적자라 역산 불가)"

    # 순현금이 없으면 0 으로 계산한다. 그 사실을 부르는 쪽에 알린다.
    nc = d.get("netcash")
    nc_missing = nc is None
    nc = nc or 0
    target_ev = mcap - nc          # 시총에서 순현금을 뺀 것이 영업가치

    if target_ev <= 0:
        return None, margin, "순현금이 시총보다 커서 역산할 수 없음"

    def ev(g):
        return dcf_value(rev0, g, margin, reinvest, tax, wacc, terminal, years)

    lo, hi = -0.30, 1.50           # 연 -30% ~ +150% 범위에서 찾는다
    v_lo, v_hi = ev(lo), ev(hi)
    if v_lo is None or v_hi is None:
        return None, margin, "자본비용이 영구성장률보다 낮음"
    if v_lo > target_ev:
        return lo, margin, "역성장을 가정해도 지금 주가보다 비쌈 (저평가 신호)"
    if v_hi < target_ev:
        return hi, margin, "연 150% 성장을 가정해도 지금 주가에 못 미침"

    for _ in range(60):            # 이분법 60회면 충분히 수렴한다
        mid = (lo + hi) / 2
        if ev(mid) < target_ev:
            lo = mid
        else:
            hi = mid
    return ((lo + hi) / 2, margin,
            "순현금 데이터 없음 (0 으로 계산)" if nc_missing else None)



def own_history(d):
    """이 회사 자신의 과거 실적에서 기준선을 뽑는다.

    업종 평균과 비교하면 파운드리와 팹리스가 섞여 의미가 흐려진다.
    그래서 "이 회사가 지난 몇 년 어땠나" 를 기준으로 삼는다.
    가정이 그 범위를 크게 벗어나면 "갑자기 잘할 거라 보는 것" 이다.

    돌려주는 값 (없으면 None)
      g_avg     최근 매출 성장률 평균
      g_min     가장 나빴던 해
      g_max     가장 좋았던 해
      m_avg     영업이익률 평균
      m_last    최근 영업이익률
      m_max     역대 최고 이익률
      reinv     재투자 효율 (매출 증가분 ÷ 설비투자+R&D)
    """
    out = {}
    revs = d.get("revs") or []
    if len(revs) >= 2:
        gs = [(revs[i + 1] / revs[i] - 1) * 100
              for i in range(len(revs) - 1) if revs[i] > 0]
        if gs:
            out["g_avg"] = sum(gs) / len(gs)
            out["g_min"] = min(gs)
            out["g_max"] = max(gs)
            out["g_list"] = gs
    m = d.get("margins") or []
    if m:
        out["m_avg"] = sum(m) / len(m)
        out["m_last"] = m[-1]
        out["m_max"] = max(m)
        out["m_list"] = m
    if d.get("reinv_eff") is not None:
        out["reinv"] = d["reinv_eff"]
    if d.get("cagr") is not None:
        out["cagr"] = d["cagr"]
    return out


def check_assumptions(g, margin, reinvest, hist):
    """가정이 이 회사의 과거와 얼마나 동떨어졌는지 본다.

    다모다란이 말하는 "스토리와 숫자가 맞물리는가" 를
    데이터로 할 수 있는 만큼만 흉내 낸 것이다.
    경고 목록을 돌려준다. 비어 있으면 무리한 가정은 아니라는 뜻.
    """
    w = []
    gp, mp = g * 100, margin * 100

    # 성장률이 과거 최고를 넘는가
    if "g_max" in hist and gp > hist["g_max"]:
        w.append(f"성장 {gp:.0f}% 는 역대 최고({hist['g_max']:.0f}%)보다 높다")
    elif "g_avg" in hist and gp > hist["g_avg"] * 1.5 and gp > 10:
        w.append(f"성장 {gp:.0f}% 는 과거 평균({hist['g_avg']:.0f}%)의 1.5배다")

    # 이익률이 역대 최고를 넘는가
    if "m_max" in hist and mp > hist["m_max"] + 3:
        w.append(f"이익률 {mp:.0f}% 는 역대 최고({hist['m_max']:.0f}%)를 넘는다")

    # 성장과 재투자가 앞뒤가 맞는가
    #   빨리 크려면 돈을 더 써야 한다. 적게 쓰면서 빨리 크는 가정은
    #   과거 재투자 효율이 아주 높았던 경우에만 말이 된다.
    if "reinv" in hist and hist["reinv"] and hist["reinv"] > 0 and gp > 0:
        need = gp / 100 / hist["reinv"]      # 필요한 재투자 강도(대략)
        if reinvest < need * 0.5:
            w.append(f"성장 {gp:.0f}% 를 재투자 {reinvest*100:.0f}% 로는 어렵다 "
                     f"(과거 효율 {hist['reinv']:.2f}배 기준)")

    # 성장은 높은데 이익률까지 오르는 조합
    if gp > 25 and "m_last" in hist and mp > hist["m_last"] + 5:
        w.append(f"고성장({gp:.0f}%)과 이익률 개선"
                 f"({hist['m_last']:.0f}→{mp:.0f}%)을 동시에 가정했다")

    return w



# ═════════════════════════════════════════════════════════════
# 연도별 재무 추이  "요즘 회사가 어떤가"
#
#   기존 두 채점(성장 잠재력·장기 보유)과는 완전히 별개다.
#   SCORE_VERSION 에 영향을 주지 않는다.
#
#   재무제표만으로 계산되는 다섯 가지를 연도별로 본다.
#   주가·시총이 필요한 항목은 과거 값을 야후가 주지 않아 뺐다.
#
#   점수는 0~100 으로 환산한다. 절대 기준이 아니라
#   "그림으로 모양을 보기 위한" 환산이다. 실제 값을 같이 보여 준다.
# ═════════════════════════════════════════════════════════════

# (이름, 지표, 계산에 쓰는 야후 항목)
#   쓰는 항목이 많을수록 하나가 빠지거나 틀어질 여지가 크다.
#   별점 대신 무엇이 필요한지를 그대로 적어 두고 판단은 사람이 한다.
YEARLY_AXES = [
    ("크고 있나",   "매출 성장률",  "매출"),
    ("잘 버나",     "영업이익률",   "매출 · 영업이익"),
    ("빚은 없나",   "부채비율",     "부채 · 자기자본"),
    ("효율적인가",  "ROIC",
     "영업이익 · 자기자본 · 부채 · 현금 · 세율(가정)"),
    ("돈이 도나",   "FCF / 매출",   "영업현금흐름 · 설비투자"),
]

# 그림(삼각형)에 쓰는 축. 계산에 쓰는 항목이 적어 덜 틀어지는 셋만.
YEARLY_TRI = ["크고 있나", "잘 버나", "빚은 없나"]


def _scale(v, lo, hi, invert=False):
    """값을 0~100 으로. invert 면 낮을수록 높은 점수."""
    if v is None:
        return None
    if not isinstance(v, (int, float)):
        try:
            v = float(str(v).replace(",", "").strip())
        except Exception:
            return None
    if v != v or v in (float("inf"), float("-inf")):      # NaN·무한대
        return None
    x = (v - lo) / (hi - lo) * 100
    x = max(0.0, min(100.0, x))
    return 100 - x if invert else x


def yearly_series(d):
    """연도별 지표를 뽑는다.

    돌려주는 값
      years   ["2022", "2023", ...]  오래된 것부터
      raw     {축이름: [값, ...]}      실제 값 (None 가능)
      score   {축이름: [0~100, ...]}   그림용 환산값
      best    {축이름: 최고 점수}        배경선에 쓴다
    """
    yrs = d.get("y_years") or []
    if not yrs:
        return None

    raw = {
        "크고 있나":   d.get("y_growth") or [],
        "잘 버나":     d.get("y_margin") or [],
        "효율적인가":  d.get("y_roic") or [],
        "빚은 없나":   d.get("y_debt") or [],
        "돈이 도나":   d.get("y_fcfm") or [],
    }
    # 길이를 연도 수에 맞춘다
    n = len(yrs)
    for k in raw:
        v = list(raw[k])
        raw[k] = ([None] * (n - len(v)) + v) if len(v) < n else v[-n:]

    # 0~100 환산 기준. 업종별로 다르지만 그림용이라 고정값을 쓴다.
    rules = {
        "크고 있나":   (-20, 50, False),
        "잘 버나":     (-10, 50, False),
        "효율적인가":  (-5, 35, False),
        "빚은 없나":   (0, 200, True),     # 낮을수록 좋다
        "돈이 도나":   (-15, 35, False),
    }
    score, best = {}, {}
    for k, (lo, hi, inv) in rules.items():
        sc = [_scale(v, lo, hi, inv) for v in raw[k]]
        score[k] = sc
        vals = [x for x in sc if x is not None]
        best[k] = max(vals) if vals else None

    return {"years": yrs, "raw": raw, "score": score, "best": best}




def tri_path(d):
    """연도별 삼각형 넓이. 앱 삼각형(크고 있나·잘 버나·빚은 없나)과 같은 환산을 쓴다.

    돌려주는 값: [(연도, 넓이 0~100, (성장, 이익률, 부채) 환산점수), ...]
      세 축이 다 있는 해만. 세 축이 모두 100 이면 넓이 100.
    넓이 = (r1·r2 + r2·r3 + r3·r1) / 3  (세 축이 120° 간격인 삼각형 넓이를 0~100 으로)
    """
    ys = yearly_series(d)
    if not ys:
        return []
    out = []
    for i, yr in enumerate(ys["years"]):
        r = [ys["score"][k][i] for k in YEARLY_TRI]
        if any(x is None for x in r):
            continue
        a, b, c = (x / 100 for x in r)
        out.append((yr, (a * b + b * c + c * a) / 3 * 100, tuple(r)))
    return out


def score_fingerprint():
    """채점 로직의 지문.

    core.py 전체가 아니라 "점수를 만드는 부분" 만 해시한다.
    화면 문구나 주석을 고쳤다고 기록이 갈리면 곤란하기 때문이다.

    이 값이 다르면 서로 다른 잣대로 매긴 점수이므로
    나란히 비교하면 안 된다. verify 가 자동으로 구분한다.
    """
    import hashlib, inspect
    parts = [str(SCORE_VERSION)]
    for fn in (score_ten, score_lt, score_damo):
        try:
            src = inspect.getsource(fn)
        except Exception:
            src = fn.__name__
        # 주석과 빈 줄은 빼고 실제 코드만 본다
        lines = []
        for ln in src.splitlines():
            t = ln.strip()
            if not t or t.startswith("#"):
                continue
            lines.append(t)
        parts.append("\n".join(lines))
    for d_ in (TEN_MAX, LT_MAX, DAMO_MAX):
        parts.append(repr(sorted(d_.items())))
    blob = "\n".join(parts).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:8]



# ═════════════════════════════════════════════════════════════
# 시장 분위기 — 화면 맨 위에 한 줄로
#
#   채점에는 들어가지 않는다. 그냥 참고용이다.
#   "오늘 시장이 어떤가" 를 종목 보기 전에 알고 들어가는 용도.
#   야후를 몇 번 더 부르므로 10분 캐시를 건다.
# ═════════════════════════════════════════════════════════════

MARKET_TICKERS = [
    ("VIX",      "^VIX",     "level"),
    ("원/달러",   "KRW=X",    "level"),
    ("나스닥",    "^IXIC",    "pct"),
    ("S&P",      "^GSPC",    "pct"),
    ("반도체",    "^SOX",     "pct"),      # 필라델피아 반도체 지수. 종목이 빠진 건지 업종이 빠진 건지
    ("미10년",    "^TNX",     "level"),
]


@cache(600)
def market_snapshot():
    """시장 지표를 한 번에 받아온다. 실패한 항목은 빼고 돌려준다."""
    out = []
    for label, tk, kind in MARKET_TICKERS:
        try:
            h = yf.Ticker(tk).history(period="5d")
            if h is None or len(h) < 1:
                continue
            # 장중엔 오늘 행 Close 가 NaN 으로 올 수 있다 (차트와 같은 문제)
            h = h.dropna(subset=["Close"])
            if len(h) < 1:
                continue
            c = h["Close"]
            last = float(c.iloc[-1])
            prev = float(c.iloc[-2]) if len(c) >= 2 else None
            chg = ((last / prev - 1) * 100) if (prev and prev > 0) else None

            if kind == "pct":
                val = f"{chg:+.1f}%" if chg is not None else "-"
            elif label == "원/달러":
                val = f"{last:,.0f}"
            elif label == "미10년":
                val = f"{last:.2f}%"
            else:
                val = f"{last:.1f}"
            out.append({"label": label, "value": val, "raw": last, "chg": chg})
        except Exception:
            continue
    return out


def vix_mood(v):
    """VIX 수준을 말로. 절대 기준은 아니고 통념을 옮긴 것이다."""
    if v is None:
        return None
    if v < 15:
        return "차분"
    if v < 20:
        return "보통"
    if v < 30:
        return "불안"
    return "공포"



# ═════════════════════════════════════════════════════════════
# 밸류 판정 — 자기 이력 백분위
#
#   PER 절대수준으로 싸다/비싸다를 가리면 주기 종목에서 거꾸로 간다.
#   MU 가 그 예다: TTM 이익률 80%, PER 21 이라 싸 보이지만
#   4년 평균 이익률로 정상화하면 PER 이 세 자리가 된다.
#   이익이 꼭지일 때 PER 이 제일 낮게 나오기 때문이다.
#
#   그래서 두 가지를 본다.
#     ① 정상화 이익   현재 매출 × 과거 평균 영업이익률
#     ② 자기 이력 밴드 그 회사가 늘 받던 값 대비 지금 어디인가
#
#   ★ 한계 — 화면에도 적는다
#     야후는 연간 재무를 4년치만 준다. 밴드 표본이 4점뿐이다.
#     "하위 30%" 가 사실상 "4개 중 1개" 다. 방향만 참고할 것.
#     EDGAR 를 붙이면 17년으로 늘릴 수 있다.
# ═════════════════════════════════════════════════════════════

@cache(86400)          # 연말 종가는 하루 한 번이면 충분하다
def year_end_prices(t, years=6):
    """연말 종가. 밸류 밴드에서 그 해 시가총액을 내는 데 쓴다.

    돌려주는 값: {"2022": 123.4, ...}  실패하면 빈 dict.
    거래일이 아닌 12월 31일도 asof 로 가장 가까운 앞 날을 쓴다.
    """
    try:
        h = yf.Ticker(t).history(period=f"{years}y", auto_adjust=True)
    except Exception:
        return {}
    if h is None or len(h) < 30:
        return {}
    try:
        c = h["Close"].copy()
        c.index = c.index.tz_localize(None)
    except Exception:
        c = h["Close"]
    out = {}
    for y in sorted({d_.year for d_ in c.index}):
        try:
            v = c[c.index.year == y]
            if len(v):
                out[str(y)] = float(v.iloc[-1])      # 그 해 마지막 거래일
        except Exception:
            continue
    return out


def value_band(d, px_hist=None):
    """자기 이력 백분위 지금 밸류가 어디쯤인가.

    px_hist : {"2022": 연말종가, ...} 형태. 없으면 밴드는 못 낸다.

    돌려주는 값 (없으면 None)
      norm_margin   정상화 영업이익률 (과거 평균)
      norm_per      정상화 PER
      cur_per       지금 PER (TTM)
      band_pct      자기 이력에서 지금이 몇 백분위인가 (0~100)
      band_n        밴드를 만든 표본 수  ★ 이게 작으면 믿지 말 것
      basis         "PER" 또는 "EV/매출"
      note          한계나 주의
    """
    out = {"norm_margin": None, "norm_per": None, "cur_per": None,
           "band_pct": None, "band_n": 0, "basis": None, "note": None}

    revs = d.get("revs") or []
    margins = d.get("margins") or []
    mcap = d.get("mcap_fin") or d.get("mcap_raw")
    if not revs or not margins or not mcap:
        out["note"] = "매출·이익률·시가총액 중 없는 것이 있다"
        return out

    # ① 정상화 이익 — 현재 매출 × 정상화 이익률
    nm = sum(margins) / len(margins)
    out["norm_margin"] = nm
    rev0 = float(revs[-1])
    if rev0 <= 0:
        out["note"] = "매출이 0 이하"
        return out

    # 적자 연도가 섞이면 PER 밴드가 깨진다 → EV/매출로 간다
    has_loss = any(m <= 0 for m in margins)
    out["basis"] = "EV/매출" if has_loss else "PER"

    if not has_loss and nm > 0:
        norm_ni = rev0 * (nm / 100) * (1 - 0.21)   # 세후. 세율은 가정이다
        if norm_ni > 0:
            out["norm_per"] = mcap / norm_ni
    if d.get("per"):
        out["cur_per"] = float(d["per"])

    # ② 자기 이력 밴드
    #    연도별 시가총액을 알아야 한다. 주가 이력이 있어야 계산된다.
    if not px_hist:
        out["note"] = "과거 주가가 없어 밴드를 내지 못했다"
        return out

    yrs = d.get("y_years") or []
    sh = d.get("shares_hist") or []       # 연도별 주식수 (없으면 최신으로 대체)
    vals = []
    for i, y in enumerate(yrs):
        px = px_hist.get(str(y))
        if px is None or i >= len(revs):
            continue
        n_sh = sh[i] if i < len(sh) and sh[i] else d.get("shares")
        if not n_sh:
            continue
        mc = px * n_sh
        if has_loss:
            if revs[i] > 0:
                vals.append(mc / revs[i])          # EV/매출 (순현금 무시)
        else:
            ni_ = revs[i] * (nm / 100) * (1 - 0.21)   # 정상화 이익으로 통일
            if ni_ > 0:
                vals.append(mc / ni_)
    out["band_n"] = len(vals)
    if len(vals) < 3:
        out["note"] = f"밴드 표본이 {len(vals)}개뿐이라 백분위를 내지 않는다"
        return out

    cur = (mcap / rev0) if has_loss else out["norm_per"]
    if cur is None:
        out["note"] = "지금 값을 낼 수 없다"
        return out
    below = sum(1 for v in vals if v < cur)
    out["band_pct"] = below / len(vals) * 100
    out["note"] = (f"표본 {len(vals)}개 (야후 연간 한도). "
                   "표본이 적어 방향만 참고할 것")
    return out


def value_verdict(d, px_hist=None, g_implied=None):
    """저평가 / 적정 / 고평가 / 모름.

    ★ 화면에 내지 않는다. snapshot 에 기록만 하고
      6개월 뒤 verify 로 쓸모를 확인한 다음에 낸다.
      "62%" 는 사람이 해석하지만 "고평가" 는 결론으로 읽힌다.

    경계는 하나로 정하지 않고 후보 여럿을 같이 돌려준다.
    6개월 뒤 어느 경계가 맞았는지 보고 고르기 위함이다.
    """
    if isinstance(d, dict):                  # 글자가 섞여 와도 안 터지게
        _numify(d, _SCORE_NUMKEYS)
        _numify_lists(d, _SCORE_LISTKEYS)
    vb = value_band(d, px_hist)
    cagr = d.get("cagr")

    reasons = []
    cheap = rich = 0

    # ① 역산 — 시장이 기대하는 성장률 대 과거 실적
    #   ★ 역산에 최근 이익률을 쓰면 주기 함정이 그대로 남는다.
    #     MU 는 최근 2년 평균이 51%(꼭지)라 기대 성장률이 낮게 나온다.
    #     그래서 정상화 이익률로 다시 역산한다.
    nm_ = vb.get("norm_margin")
    used_m = None                        # 실제로 거꾸로 계산에 쓴 이익률 (%)
    if nm_ is not None and nm_ <= 0:
        # 과거 평균이 적자면 그걸로는 어떤 성장률도 주가를 설명 못 한다 → 다시 계산 안 함
        reasons.append(f"과거 4년 평균 이익률이 {nm_:.0f}% (적자)라 최근 이익률로 계산했다")
    if nm_ is not None and nm_ > 0:
        try:
            g2, _, _ = implied_growth(d, margin=nm_ / 100)
            if g2 is not None:
                g_implied = g2
                used_m = nm_
                reasons.append(f"과거 4년 평균 이익률 {nm_:.0f}% 로 다시 계산했다")
        except Exception:
            pass

    gap = None
    if g_implied is not None and cagr is not None:
        gap = g_implied * 100 - cagr      # 양수면 기대가 과거보다 높다
        if gap < 0:
            cheap += 1
            reasons.append(f"바라는 성장률이 실제로 커 온 속도보다 {abs(gap):.0f}%p 낮다")
        else:
            rich += 1
            reasons.append(f"바라는 성장률이 실제로 커 온 속도보다 {gap:.0f}%p 높다")

    # ② 자기 이력 밴드
    bp = vb.get("band_pct")
    if bp is not None:
        if bp <= 30:
            cheap += 1
            reasons.append(f"이 회사 과거 중 {bp:.0f}번째 — 싼 쪽 ({vb['basis']})")
        elif bp >= 70:
            rich += 1
            reasons.append(f"이 회사 과거 중 {bp:.0f}번째 — 비싼 쪽 ({vb['basis']})")
        else:
            reasons.append(f"이 회사 과거 중 {bp:.0f}번째 — 중간 ({vb['basis']})")

    # ★ 두 가지를 다 못 봤으면 판단하지 않는다.
    #   전에는 거꾸로 계산 하나만으로 "고평가" 라고 했다.
    #   견줄 과거가 없는데 비싸다고 하는 건 근거가 부족하다.
    n_signal = (1 if gap is not None else 0) + (1 if bp is not None else 0)
    if cheap and rich:
        label = "모름"
        reasons.append("두 가지가 서로 반대로 나왔다")
    elif n_signal < 2:
        label = "모름"
        if bp is None:
            reasons.append("견줄 과거 자료가 없어 한쪽만 봤다")
        else:
            reasons.append("따져 볼 게 하나뿐이다")
    elif cheap >= 1 and rich == 0:
        label = "저평가 쪽"
    elif rich >= 1 and cheap == 0:
        label = "고평가 쪽"
    else:
        label = "모름"
        reasons.append("따져 볼 게 없다")

    # 경계 후보를 여러 개 같이 남긴다 (나중에 어느 것이 맞았는지 보려고)
    cand = {}
    if bp is not None:
        for th in (20, 30, 40):
            cand[f"band_lo{th}"] = bp <= th
            cand[f"band_hi{th}"] = bp >= 100 - th
    if gap is not None:
        for th in (0, 5, 10):
            cand[f"gap_over{th}"] = gap > th

    return {"label": label, "reasons": reasons,
            # 화면이 같은 값을 쓰도록 실제 쓴 것을 돌려준다.
            # 화면이 따로 계산하면 "30%p 높다" 와 "23%p 낮다" 가 같이 나온다.
            "implied_g": g_implied,
            # 실제로 쓴 이익률만 돌려준다. 다시 계산 안 했으면 None →
            # 화면은 처음 계산의 이익률을 쓴다 (전엔 여기서 -8% 같은 엉뚱한 이름표가 붙었다)
            "used_margin": used_m,
            "band_pct": bp, "band_n": vb.get("band_n"),
            "basis": vb.get("basis"), "norm_margin": vb.get("norm_margin"),
            "norm_per": vb.get("norm_per"), "cur_per": vb.get("cur_per"),
            "implied_gap": gap, "note": vb.get("note"), "cand": cand}


def damo_verdict(p):
    """화면에서는 안 쓴다(다모다란은 값만 보여 준다).
    history 기록과 정렬에만 남아 있다."""
    if p >= 75: return "상위권", ORANGE
    if p >= 55: return "중상위", AMBER
    if p >= 35: return "중하위", SLATE
    return "하위권", MUTED


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

    # 실적 대조용 — 분기 매출·영업이익을 날짜와 함께 남긴다
    q_hist = []
    try:
        if qr is not None:
            for dt in qr.index:                      # _row 가 오래된→최신 정렬
                r_ = _at(qr, dt)
                if r_ is None or r_ <= 0:
                    continue
                o_ = _at(qo, dt) if qo is not None else None
                q_hist.append({"date": str(dt)[:10], "rev": r_,
                               "op": o_,
                               "margin": (o_ / r_ * 100) if o_ is not None else None})
    except Exception:
        q_hist = []
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

    # 연도별 주식수 — 밸류 밴드에서 그 해 시가총액을 내는 데 쓴다
    shares_hist = []
    try:
        sh_a = _row(bs, ["Ordinary Shares Number", "Share Issued"])
        if sh_a is not None and rev is not None:
            for dt in rev.index:
                v_ = _at(sh_a, dt)
                shares_hist.append(v_)
    except Exception:
        shares_hist = []
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

    # ── 연도별 추이 ("요즘 회사가 어떤가" 용) ──
    # 재무제표만으로 계산되는 다섯 가지를 연도별로 만든다.
    # 주가·시총이 필요한 항목은 과거 값이 없어 넣지 않는다.
    y_years, y_growth, y_margin, y_roic, y_debt, y_fcfm = [], [], [], [], [], []
    try:
        if rev is not None:
            rv = rev.dropna().sort_index()          # 오래된 것 → 최신
            dates = list(rv.index)
            y_years = [str(x)[:4] for x in dates]

            tax_r = info.get("effectiveTaxRate")
            if not (isinstance(tax_r, (int, float)) and 0 <= tax_r < 0.6):
                tax_r = 0.21

            cash_row = _row(bs, ["Cash Cash Equivalents And Short Term Investments",
                                 "Cash And Cash Equivalents"])
            debt_row = _row(bs, ["Total Debt"])

            for i, dt_ in enumerate(dates):
                r_ = float(rv.iloc[i])

                # 매출 성장률
                if i == 0 or float(rv.iloc[i - 1]) <= 0:
                    y_growth.append(None)
                else:
                    y_growth.append((r_ / float(rv.iloc[i - 1]) - 1) * 100)

                # 영업이익률
                o_ = _at(op, dt_)
                y_margin.append(o_ / r_ * 100 if (o_ is not None and r_ > 0) else None)

                # ROIC
                e_ = _at(eq, dt_)
                dd_ = _at(debt_row, dt_)
                cc_ = _at(cash_row, dt_)
                if o_ is not None and e_ is not None:
                    inv_ = e_ + (dd_ or 0) - (cc_ or 0)
                    y_roic.append(o_ * (1 - tax_r) / inv_ * 100 if inv_ > 0 else None)
                else:
                    y_roic.append(None)

                # 부채비율
                y_debt.append(dd_ / e_ * 100
                              if (dd_ is not None and e_ and e_ > 0) else None)

                # FCF / 매출
                oc_ = _at(ocf_a, dt_)
                cx_ = _at(capex, dt_)
                if oc_ is not None and r_ > 0:
                    y_fcfm.append((oc_ + (cx_ or 0)) / r_ * 100)
                else:
                    y_fcfm.append(None)
    except Exception:
        y_years = []

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
                    dt_o = oi.index[-1]
                    c_ = 0.0
                    if ci is not None and dt_o in ci.index:
                        c_ = float(ci[dt_o])
                    elif ci is not None:
                        c_ = None          # 같은 연도 설비투자가 없으면 포기
                    if c_ is not None:
                        fcf_last = ocf_last + c_
    except Exception:
        pass

    # R&D 집중도 (매출 대비 %)
    rnd = None
    # 야후는 회사마다 행 이름이 다르다. 후보를 여러 개 준다.
    rd_ = _row(inc, ["Research And Development",
                     "Research Development",
                     "Research & Development"])
    if rd_ is not None and rev is not None:
        try:
            rv = float(rev.iloc[-1])
            if rv > 0:
                rnd = float(rd_.iloc[-1]) / rv * 100
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
    roic_note = None
    roic_asof = None
    try:
        if op is not None and eq is not None:
            oi_ = op.dropna().sort_index()
            eq_ = eq.dropna().sort_index()
            common = [x for x in oi_.index if x in eq_.index]
            if common:
                dt_ = common[-1]
                ebit = float(oi_[dt_])

                # 실효세율. 야후가 안 주면 계산은 하되 가정했다고 남긴다.
                # 없는 값을 조용히 채우면 나중에 왜 틀렸는지 알 수 없다.
                tax = info.get("effectiveTaxRate")
                if isinstance(tax, (int, float)) and 0 <= tax < 0.6:
                    tax_assumed = False
                else:
                    tax, tax_assumed = 0.21, True
                nopat = ebit * (1 - tax)

                equity_ = float(eq_[dt_])
                debt_t = _row(bs, ["Total Debt"])
                cash_t = _row(bs, ["Cash Cash Equivalents And Short Term Investments",
                                   "Cash And Cash Equivalents"])

                d_ = _at(debt_t, dt_)
                c_ = _at(cash_t, dt_)

                notes = []
                if tax_assumed:
                    notes.append("세율 21% 가정")
                if d_ is None:
                    notes.append("부채 데이터 없음")
                if c_ is None:
                    notes.append("현금 데이터 없음")

                invested = equity_ + (d_ or 0) - (c_ or 0)
                if invested > 0:
                    roic = nopat / invested * 100
                    roic_note = " · ".join(notes) if notes else None
                    roic_asof = str(dt_)[:10]
    except Exception:
        pass

    # ② 재투자 효율 = 매출 증가분 / (설비투자 + R&D)
    #    같은 돈을 써서 매출을 얼마나 늘렸나.
    #    적자 회사도 계산되므로 초기 성장주를 볼 수 있다.
    reinv_eff = None
    reinv_note = None
    try:
        if rev is not None:
            rv = rev.dropna().sort_index()
            if len(rv) >= 2:
                d_rev = float(rv.iloc[-1]) - float(rv.iloc[-2])
                dt_ = rv.index[-1]
                spend = 0.0
                parts = []
                if capex is not None and dt_ in capex.index:
                    try:
                        if not pd.isna(capex[dt_]):
                            spend += abs(float(capex[dt_]))
                            parts.append("설비투자")
                    except Exception:
                        pass
                if rd_ is not None and dt_ in rd_.index:
                    try:
                        if not pd.isna(rd_[dt_]):
                            spend += abs(float(rd_[dt_]))
                            parts.append("R&D")
                    except Exception:
                        pass
                if spend > 0:
                    reinv_eff = d_rev / spend
                    # 둘 중 하나만 있으면 반쪽 계산이다. 그렇다고 밝힌다.
                    reinv_note = (None if len(parts) == 2
                                  else f"{parts[0]}만 반영")
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
            # 계산값이 없으면(배당금 미제공) 그대로 퍼센트로 본다.
            # 100 을 곱해 8% 같은 값을 만드는 것보다 안전하다.
            dy_field = raw

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
    ocf_s = _sum_last(ocf_q)          # 최근 4분기 합 (_row 가 정렬해 둔다)
    ni_s = _sum_last(ni_q)

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

    # 순현금 = 현금 - 총부채
    netcash = None
    netcash_note = None
    try:
        cash = _last(qb, ["Cash Cash Equivalents And Short Term Investments",
                            "Cash And Cash Equivalents"])
        debt_q = _last(qb, ["Total Debt"])
        if cash is None:                      # 분기에 없으면 연간으로
            cash = _last(bs, ["Cash Cash Equivalents And Short Term Investments",
                                "Cash And Cash Equivalents"])
        if debt_q is None:
            debt_q = _last(bs, ["Total Debt"])
        if cash is not None:
            netcash = cash - (debt_q or 0)
            # 부채 데이터가 없으면 0 으로 계산한 것이다.
            # 무차입 회사라 그럴 수도 있고 야후가 안 준 것일 수도 있다.
            if debt_q is None:
                netcash_note = "부채 데이터 없음 (0 으로 계산)"
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

    # ★ 야후가 숫자 자리에 글자를 주는 종목이 있다.
    #   1,004개로 넓히니 ZSQR 에서 per 이 문자열로 와서
    #   "'>' not supported between 'str' and 'int'" 로 터졌다.
    #   돌려주기 전에 숫자여야 하는 값을 전부 숫자로 만든다.
    def _n(v):
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            try:
                return None if pd.isna(v) else float(v)
            except Exception:
                return float(v)
        try:
            return float(str(v).replace(",", "").strip())
        except Exception:
            return None

    _out = {
        "ticker": t.upper(),
        "name": info.get("shortName") or info.get("longName"),
        "sector": info.get("sector"),
        # 회사 개요 (채점과 무관, 화면용). 야후 설명은 영어로 온다.
        "industry": info.get("industry"),
        "country": info.get("country"),
        "employees": info.get("fullTimeEmployees"),
        "website": info.get("website"),
        "summary": info.get("longBusinessSummary"),
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
        "shares_hist": shares_hist,
        "y_years": y_years, "y_growth": y_growth, "y_margin": y_margin,
        "y_roic": y_roic, "y_debt": y_debt, "y_fcfm": y_fcfm,
        "cagr": cagr, "dil_annual": dil_annual, "dilution": dilution,
        "ocf": ocf_s, "ni": ni_s, "cover": cover, "netcash": netcash,
        "netcash_note": netcash_note,
        "beats": beats,
        "qgrowth": qgrowth, "q_hist": q_hist,
        "rnd": rnd, "dy": dy, "div_yrs": div_yrs,
        "roic": roic, "roic_note": roic_note, "roic_asof": roic_asof,
        "reinv_eff": reinv_eff, "reinv_note": reinv_note,
        "fcf_last": fcf_last, "ocf_last": ocf_last,
        "capex_last": capex_last, "capex_chg": capex_chg, "bvps_chg": bvps_chg,
        "tgt": info.get("targetMeanPrice"),
        "n_analyst": info.get("numberOfAnalystOpinions"),
        "rec": info.get("recommendationKey"),
        "short_pct": pct(info.get("shortPercentOfFloat")),
        "earnings": info.get("earningsTimestamp"),
    }

    # 숫자여야 하는 값
    for _k in ("price", "chg", "mcap", "mcap_krw", "mcap_fin", "mcap_raw",
               "margin", "debt", "debt_chg", "debt_yf", "insider", "inst",
               "roe", "per", "peg", "growth", "cagr", "rev1y", "px1y", "dd",
               "dilution", "dil_annual", "rnd", "dy", "netcash", "ocf", "ni",
               "roic", "reinv_eff", "tgt", "short_pct", "fcf", "beta",
               "div_yrs", "n_analyst", "shares", "earnings"):
        if _k in _out:
            _out[_k] = _n(_out[_k])
    return _out


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
    """획득 점수, 만점, 백분율.

    점수가 None 인 항목은 "데이터가 없어 채점 못 함" 이라는 뜻이다.
    분자에도 분모에도 넣지 않는다. 화면에는 이유와 함께 보여 주되
    % 계산에서는 빠진다. 0 으로 치면 모르는 회사가 나쁜 회사가 된다.
    """
    scored = [(k, s) for k, s, _ in items if s is not None]
    got = sum(s for _, s in scored)
    avail = sum(mx[k] for k, _ in scored)
    return got, avail, (got / avail * 100 if avail else 0)


def ten_verdict(p):
    """점수 구간을 말로 옮긴 것뿐이다.

    ★ "후보군" "제외" 같은 말은 뺐다.
      점수가 높다고 오른다는 근거가 이 안에 없다. 사라/팔라가 아니다.
      위쪽은 "우리 잣대로 위쪽" 이라는 사실만 말한다.
      아래쪽(하위권)은 여러 항목이 동시에 나쁘다는 뜻이라 근거가 좀 있다.
    ★ 경계 70/52/35 도 임의값이다.
    """
    if p >= 70: return "상위권", ORANGE
    if p >= 52: return "중상위", AMBER
    if p >= 35: return "중하위", SLATE
    return "하위권", MUTED


def lt_verdict(p):
    """위와 같다. "핵심 보유" "부적합" 은 판단을 대신 내리는 말이라 뺐다."""
    if p >= 75: return "상위권", ORANGE
    if p >= 58: return "중상위", AMBER
    if p >= 40: return "중하위", SLATE
    return "하위권", MUTED




# ═════════════════════════════════════════════════════════════
# SEC 공시 (EDGAR)
#
#   회사가 법적 책임을 지고 낸 원문이다. 뉴스보다 확실하고 빠르다.
#   8-K 는 실적발표·대형계약·경영진 변경 같은 것을 며칠 안에 내야 한다.
#   기자가 기사를 쓰기 전에 여기 먼저 올라온다.
#
#   ★ SEC 는 인증이 필요 없지만 User-Agent 에 연락처를 요구한다.
#     안 넣으면 차단된다.
#   ★ 미국 상장사만 된다. 해외 ADR(TSM·ASML)은 20-F·6-K 만 낸다.
# ═════════════════════════════════════════════════════════════

SEC_UA = "gyong-screener (contact: zztzzt0086@gmail.com)"

# 8-K 항목 번호의 뜻. 번호만 보면 무슨 일인지 모른다.
ITEM_KIND = {
    "1.01": "중요 계약 체결",
    "1.02": "중요 계약 해지",
    "2.01": "자산 취득·처분 완료",
    "2.02": "실적 발표",
    "2.03": "채무 발생",
    "3.01": "상장 규정 위반·상장폐지 통보",
    "3.02": "미등록 주식 발행",
    "4.01": "회계법인 변경",
    "4.02": "과거 재무제표 신뢰 불가",
    "5.02": "임원 선임·사임",
    "5.07": "주주총회 판정",
    "7.01": "공정공시 자료",
    "8.01": "기타 중요 사항",
    "9.01": "첨부 서류",
}


def item_text(items):
    """8-K 항목 번호를 말로. "2.02,9.01" → "실적 발표" """
    if not items:
        return ""
    out = []
    for x in str(items).split(","):
        x = x.strip()
        if x == "9.01":          # 첨부 서류는 내용이 없다
            continue
        t = ITEM_KIND.get(x)
        if t:
            out.append(t)
    return " · ".join(out)


# 무슨 서류인지 한 줄로
FILING_KIND = {
    "8-K":   "수시공시 (실적·계약·경영진 등)",
    "10-Q":  "분기보고서",
    "10-K":  "연간보고서",
    "S-1":   "증권신고서 (신규 상장·증자)",
    "S-3":   "증권신고서 (추가 발행)",
    "424B5": "증권 발행 확정",
    "DEF 14A": "위임장 (주총 안건·임원 보수)",
    "SC 13D": "5% 이상 취득 (경영참여 목적)",
    "SC 13G": "5% 이상 취득 (단순투자)",
    "4":     "임원·대주주 매매",
    "3":     "임원·대주주 최초 보고",
    "5":     "임원·대주주 연간 보고",
    "144":   "대주주 매도 예정 신고",
    "SC 13D/A": "5% 취득 변경 (경영참여)",
    "SC 13G/A": "5% 취득 변경 (단순투자)",
    "SCHEDULE 13G/A": "5% 이상 보유 변경 (단순투자)",
    "SCHEDULE 13D/A": "5% 이상 보유 변경 (경영참여)",
    "SCHEDULE 13G": "5% 이상 보유 (단순투자)",
    "SCHEDULE 13D": "5% 이상 보유 (경영참여)",
    "8-K/A": "수시공시 수정본",
    "10-Q/A": "분기보고서 수정본",
    "10-K/A": "연간보고서 수정본",
    "11-K":  "임직원 주식매입제도 보고",
    "S-8":   "임직원 주식 발행 신고",
    "25-NSE": "상장 폐지 신고",
    "20-F":  "외국기업 연간보고서",
    "6-K":   "외국기업 수시보고",
}


@cache(86400)
def _sec_ticker_map():
    """티커 → CIK(회사 고유번호). 하루 한 번이면 충분하다."""
    import urllib.request
    try:
        req = urllib.request.Request(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": SEC_UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = json.load(r)
    except Exception:
        return {}
    out = {}
    for v in raw.values():
        t = str(v.get("ticker", "")).upper()
        if t:
            out[t] = str(v.get("cik_str", "")).zfill(10)
    return out


# 뒤로 미는 서류.
#   임원 매매(Form 3/4/5)와 매도예정(144)은 하루에도 수십 건 올라와
#   중요한 공시를 밀어낸다. 빼지는 않고 아래로 내린다.
NOISE_FORMS = {"3", "4", "5", "144",
               "3/A", "4/A", "5/A", "144/A"}


def _is_noise(form):
    """임원 매매·매도예정이면 True. 수정본(/A)도 같이 본다."""
    f = str(form).strip().upper()
    if f in NOISE_FORMS:
        return True
    base = f.split("/")[0]
    return base in {"3", "4", "5", "144"}


def split_filings(fl):
    """중요 공시와 임원 매매로 가른다.

    돌려주는 값: (중요, 임원매매)
    """
    main = [x for x in fl if not _is_noise(x["form"])]
    insider = [x for x in fl if _is_noise(x["form"])]
    return main, insider



# ═════════════════════════════════════════════════════════════
# EDGAR 재무 (XBRL companyfacts)
#
#   회사가 SEC 에 낸 10-K 원본 숫자. 야후는 4년치뿐이지만 여기는 보통 15년 넘게 있다.
#   1단계: 연간 숫자만 받아 오고 야후와 대조한다. 점수에는 아직 안 쓴다.
#
#   ★ 알고 쓸 것
#     · 미국 회계기준(us-gaap)으로 내는 회사만 된다. TSM·ASML 같은 IFRS 회사는 빈다.
#     · 회사가 해마다 태그를 바꾼다 (예: 2018년 SalesRevenueNet → RevenueFromContract...).
#       그래서 후보 태그를 여러 개 두고, 결산일마다 먼저 나오는 걸 쓴다.
#     · 같은 결산일 숫자가 다음 해 10-K 에 비교용으로 또 나온다 (재작성 포함).
#       가장 나중에 낸 값을 쓴다.
#     · 부채는 태그 조합이 회사마다 달라 근사치다.
# ═════════════════════════════════════════════════════════════

EDGAR_ANNUAL_FORMS = {"10-K", "10-K/A", "10-KT", "20-F", "20-F/A", "40-F", "40-F/A"}

EDGAR_TAGS = {
    # 한 해 동안 쌓이는 값 (기간이 1년인 것만)
    "rev": ["Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueNet", "SalesRevenueGoodsNet", "SalesRevenueServicesNet"],
    "op": ["OperatingIncomeLoss"],
    "ni": ["NetIncomeLoss", "ProfitLoss"],
    "pretax": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
               "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "tax": ["IncomeTaxExpenseBenefit"],
    "rnd": ["ResearchAndDevelopmentExpense",
            "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"],
    "intexp": ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"],
    "shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "divpaid": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
    # 영업이익 줄을 표준 태그로 안 적는 해가 있다 (COHR 2025·2026 등) → 계산용
    "gp": ["GrossProfit"],
    "opex": ["OperatingExpenses"],
    # 결산일 하루의 값
    "eq": ["StockholdersEquity",
           "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "sti": ["ShortTermInvestments", "MarketableSecuritiesCurrent",
            "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
            "AvailableForSaleSecuritiesCurrent"],
    "debt_total": ["LongTermDebt",
                   "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities"],
    "debt_nc": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
    "debt_cur": ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent",
                 "DebtCurrent"],
    "debt_st": ["ShortTermBorrowings", "CommercialPaper"],
    # 자본과 부채 사이에 따로 적는 우선주 등 (COHR 의 베인 우선주가 여기 있다)
    #   10-K 자기자본에는 빠지고 야후는 자본에 더해 보여 준다.
    "temp_eq": ["TemporaryEquityCarryingAmountAttributableToParent",
                "TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests"],
}
EDGAR_INSTANT = {"eq", "cash", "sti", "debt_total", "debt_nc", "debt_cur", "debt_st",
                 "temp_eq"}


def _edgar_pick(gaap, tags, instant, unit):
    """태그 후보들에서 결산일별 값을 고른다.

    돌려주는 값: ({결산일: 값}, {결산일: 쓴 태그}, {결산일: 그 값이 실린 보고서 낸 날})
    """
    from datetime import date as _d
    vals, used, filed_at = {}, {}, {}
    for tag in tags:
        node = gaap.get(tag)
        if not node:
            continue
        units = node.get("units") or {}
        rows = units.get(unit)
        if rows is None:
            continue
        best = {}                                   # 결산일 → (filed, val)
        for r in rows:
            if r.get("form") not in EDGAR_ANNUAL_FORMS:
                continue
            end = r.get("end")
            if not end or r.get("val") is None:
                continue
            if not instant:
                st_ = r.get("start")
                if not st_:
                    continue
                try:
                    days = (_d.fromisoformat(end) - _d.fromisoformat(st_)).days
                except Exception:
                    continue
                if not (340 <= days <= 380):        # 1년짜리만 (52·53주 회계연도 포함)
                    continue
            filed = r.get("filed") or ""
            if end not in best or filed > best[end][0]:
                best[end] = (filed, r["val"])
        for end, (fd, v) in best.items():
            if end not in vals:                     # 앞 순위 태그가 이미 있으면 그대로
                vals[end] = v
                used[end] = tag
                filed_at[end] = fd
    return vals, used, filed_at


@cache(86400)
def edgar_annual(t, max_years=17):
    """EDGAR 연간 재무. 실패하면 빈 dict.

    돌려주는 값
      entity  회사명      currency  통화
      years   [{"end", "rev", "op", "ni", "pretax", "tax", "rnd", "ocf", "capex",
                "intexp", "shares", "eq", "cash", "sti", "debt"}, ...]  오래된 순
      tags    {항목: {결산일: 쓴 태그}}   어느 태그에서 왔는지 (검산용)
    """
    import urllib.request
    from datetime import date as _d
    cik = _sec_ticker_map().get(str(t).upper())
    if not cik:
        return {}
    try:
        req = urllib.request.Request(
            f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
            headers={"User-Agent": SEC_UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
    except Exception:
        return {}

    gaap = (data.get("facts") or {}).get("us-gaap") or {}
    if not gaap:
        return {}

    # 통화: USD 가 있으면 USD
    cur = "USD"
    for tg in EDGAR_TAGS["rev"] + EDGAR_TAGS["ni"]:
        u = (gaap.get(tg) or {}).get("units") or {}
        if u:
            cur = "USD" if "USD" in u else next(iter(u))
            break

    raw, tags, filed = {}, {}, {}
    for key, cands in EDGAR_TAGS.items():
        unit = "shares" if key == "shares" else cur
        raw[key], tags[key], filed[key] = _edgar_pick(gaap, cands,
                                                      key in EDGAR_INSTANT, unit)

    # 결산일 = 매출이나 순이익이 1년치로 잡힌 날
    ends = sorted(set(raw["rev"]) | set(raw["ni"]))
    if not ends:
        return {}

    def _inst(key, end):
        v = raw[key].get(end)
        if v is not None:
            return v
        # 결산일이 하루이틀 어긋나게 찍힌 회사가 있다 → 7일 안이면 같은 날로 본다
        try:
            e0 = _d.fromisoformat(end)
        except Exception:
            return None
        for k2, v2 in raw[key].items():
            try:
                if abs((_d.fromisoformat(k2) - e0).days) <= 7:
                    return v2
            except Exception:
                continue
        return None

    years = []
    for end in ends[-max_years:]:
        y = {"end": end}
        for key in ("rev", "op", "ni", "pretax", "tax", "rnd", "ocf", "capex",
                    "intexp", "shares", "divpaid"):
            y[key] = raw[key].get(end)
        # 주식 수가 실린 보고서를 낸 날 — 그 뒤의 액면분할만 곱해 맞추려고 남긴다
        y["shares_filed"] = filed["shares"].get(end)
        # 영업이익이 비면 매출총이익 − 영업비용으로 채우고 표시한다
        #   ★ "매출 − 총비용" 은 쓰지 않는다. COHR 처럼 총비용에 이자·기타손익을
        #     섞어 적는 회사는 그렇게 빼면 세전이익이 나온다 (2026-09-24 확인).
        #   ★ 계산값이 세전이익 + 이자비용의 절반보다 작으면 이자가 섞였다는 뜻 → 버린다.
        #     틀린 숫자를 채우느니 비워 두는 게 낫다.
        y["op_calc"] = False
        if y["op"] is None:
            gp, ox = raw["gp"].get(end), raw["opex"].get(end)
            if gp is not None and ox is not None:
                cand = gp - ox
                ok = True
                if y.get("pretax") is not None and y.get("intexp"):
                    if cand < y["pretax"] + 0.5 * abs(y["intexp"]):
                        ok = False
                if ok:
                    y["op"], y["op_calc"] = cand, True
        for key in ("eq", "cash", "sti", "temp_eq"):
            y[key] = _inst(key, end)
        tot, nc, cu, stb = (_inst(k, end) for k in
                            ("debt_total", "debt_nc", "debt_cur", "debt_st"))
        if tot is not None:
            y["debt"] = tot + (stb or 0)
        elif nc is not None or cu is not None:
            y["debt"] = (nc or 0) + (cu or 0) + (stb or 0)
        else:
            y["debt"] = stb
        if y["capex"] is not None:
            y["capex"] = abs(y["capex"])
        years.append(y)

    # 부채 태그를 한 해도 안 적었고 이자비용도 한 번도 없으면 → 빚이 없는 회사로 보고 0.
    #   이자를 냈는데 부채 태그가 없으면 태그를 못 찾은 것이라 빈 칸 그대로 둔다
    #   (그냥 0 으로 채우면 빚 있는 회사 점수가 부풀려진다).
    debt_tags = ("debt_total", "debt_nc", "debt_cur", "debt_st")
    no_debt_tag = all(not raw[k] for k in debt_tags)
    no_interest = all(not y.get("intexp") for y in years)
    for y in years:
        y["debt_zero"] = False
        if y.get("debt") is None and no_debt_tag and no_interest:
            y["debt"], y["debt_zero"] = 0.0, True
    return {"entity": data.get("entityName"), "currency": cur,
            "years": years, "tags": tags}



@cache(86400)
def price_history_full(t):
    """상장 이후 전체 일봉 종가(분할 반영)·배당·분할. 실패하면 빈 dict.

    ★ 야후 과거 종가는 액면분할을 반영해 고쳐져 있다 (배당은 반영 안 한 Close).
    """
    try:
        tk = yf.Ticker(t)
        h = tk.history(period="max", auto_adjust=False)
    except Exception:
        return {}
    if h is None or h.empty:
        return {}
    h = h.dropna(subset=["Close"])
    out = {"close": {}, "div": {}, "split": {}}
    for d_, v in zip(h.index, h["Close"]):
        out["close"][str(pd.Timestamp(d_).date())] = float(v)
    for name, key in (("dividends", "div"), ("splits", "split")):
        try:
            s_ = getattr(tk, name)
            if s_ is not None and len(s_):
                for d_, v in s_.items():
                    if v:
                        out[key][str(pd.Timestamp(d_).date())] = float(v)
        except Exception:
            pass
    return out


def edgar_year_inputs(t):
    """해마다 채점 함수에 넣을 입력(d)을 만든다. 지금 채점 함수를 그대로 쓰기 위한 것.

    돌려주는 값: [(결산일, d, 메모), ...]  오래된 순. 4년치가 모인 해부터.

    ★ 그해 10-K 숫자 + 그해 결산일 주가만 쓴다. 지금 값은 섞지 않는다.
    ★ 과거에 못 구하는 것은 비운다 → 채점 함수가 그 항목만 뺀다
        insider(내부자 지분·지금 값뿐) · qmargin/qgrowth(분기 자료 없음)
        peg(애널 추정치) → 채점 함수의 "PER ÷ 연평균 성장" 경로로 넘어간다
    ★ 주식 수는 10-K 를 낸 날 이후의 액면분할만 곱해 지금 기준으로 맞춘다.
      (분할 뒤에 낸 10-K 는 옛 연도 주식 수도 이미 고쳐 싣는다)
    """
    from datetime import date as _d, timedelta as _td
    import bisect
    e = edgar_annual(t)
    ys = (e or {}).get("years") or []
    if len(ys) < 4:
        return []
    ph = price_history_full(t) or {}
    closes = ph.get("close") or {}
    cdates = sorted(closes)
    splits = sorted((ph.get("split") or {}).items())
    divs = sorted((ph.get("div") or {}).items())

    def px_on(day):                      # 그날 또는 그 전 마지막 종가 (10일 안)
        if not cdates:
            return None
        k = bisect.bisect_right(cdates, day) - 1
        if k < 0:
            return None
        try:
            if (_d.fromisoformat(day) - _d.fromisoformat(cdates[k])).days > 10:
                return None
        except Exception:
            return None
        return closes[cdates[k]]

    def px_max(a, b):                    # a~b 사이 최고 종가
        i0 = bisect.bisect_left(cdates, a)
        i1 = bisect.bisect_right(cdates, b)
        seg = [closes[x] for x in cdates[i0:i1]]
        return max(seg) if seg else None

    def adj_shares(y):
        s_ = y.get("shares")
        if not s_:
            return None
        cut = y.get("shares_filed") or y["end"]
        f = 1.0
        for sd, r in splits:
            if sd > cut and r > 0:
                f *= r
        return s_ * f

    def ago(day, days):
        return str(_d.fromisoformat(day) - _td(days=days))

    def pct(a, b):
        try:
            return (a / b - 1) * 100 if (a is not None and b) else None
        except Exception:
            return None

    fx = USD_KRW if (e.get("currency") or "USD") == "USD" else None
    out = []
    # 점수용 자기자본 = 10-K 자기자본 + 자본·부채 사이 우선주 등.
    #   야후(지금 점수)가 이렇게 계산하므로 같은 기준이어야 과거→지금이 이어진다.
    def eqx(w):
        e_ = w.get("eq")
        if e_ is None:
            return None
        return e_ + (w.get("temp_eq") or 0)
    ys = [dict(w, eq=eqx(w)) for w in ys]

    for k in range(3, len(ys)):
        win = ys[k - 3:k + 1]
        y, yp = ys[k], ys[k - 1]
        end = y["end"]
        p = px_on(end)
        sh, shp = adj_shares(y), adj_shares(yp)
        mcap = p * sh if (p and sh) else None

        revs = [w["rev"] for w in win if w.get("rev")]
        margins = [w["op"] / w["rev"] * 100 for w in win
                   if w.get("op") is not None and w.get("rev")]
        roes = [w["ni"] / w["eq"] * 100 for w in win
                if w.get("ni") is not None and w.get("eq") and w["eq"] > 0]
        fcfs = [w["ocf"] - (w.get("capex") or 0) for w in win
                if w.get("ocf") is not None]
        cagr = None
        if len(revs) >= 2 and revs[0] > 0 and revs[-1] > 0:
            cagr = ((revs[-1] / revs[0]) ** (1 / (len(revs) - 1)) - 1) * 100
        shs = [adj_shares(w) for w in win]
        shs = [s_ for s_ in shs if s_]
        dil_annual = None
        if len(shs) >= 2 and shs[0] > 0:
            dil_annual = ((shs[-1] / shs[0]) ** (1 / (len(shs) - 1)) - 1) * 100

        debt, eq = y.get("debt"), y.get("eq")
        cash = (y.get("cash") or 0) + (y.get("sti") or 0)
        netcash = cash - (debt or 0) if (y.get("cash") is not None or y.get("sti") is not None) else None
        bv, bvp = (eq / sh if (eq and sh) else None), (yp.get("eq") / shp if (yp.get("eq") and shp) else None)

        # 배당: 결산일 전 1년 합 ÷ 결산일 주가, 연속 증가 해 수 (지금 계산과 같은 방식)
        dps = sum(v for d_, v in divs if ago(end, 365) < d_ <= end)
        dy = dps / p * 100 if (p and dps) else None
        yearly = {}
        for d_, v in divs:
            if d_ <= end:
                yearly[d_[:4]] = yearly.get(d_[:4], 0) + v
        vals_ = [yearly[x] for x in sorted(yearly)]
        n = 0
        for i in range(len(vals_) - 1, 0, -1):
            if vals_[i] > vals_[i - 1]:
                n += 1
            else:
                break

        d = {
            "revs": revs, "margins": margins, "roes": roes, "fcfs": fcfs,
            "cagr": cagr, "growth": cagr,
            "margin": (y["op"] / y["rev"] * 100) if (y.get("op") is not None and y.get("rev")) else None,
            "roe": (y["ni"] / eq * 100) if (y.get("ni") is not None and eq and eq > 0) else None,
            "debt": (debt / eq * 100) if (debt is not None and eq and eq > 0) else None,
            "debt_asof": f"연간 {end}",
            "debt_chg": pct(debt, yp.get("debt")),
            "ocf": y.get("ocf"), "ni": y.get("ni"),
            "ocf_last": y.get("ocf"), "capex_last": y.get("capex"),
            "fcf_last": (y["ocf"] - (y.get("capex") or 0)) if y.get("ocf") is not None else None,
            "capex_chg": pct(y.get("capex"), yp.get("capex")),
            "cover": (y["op"] / abs(y["intexp"])) if (y.get("op") is not None and y.get("intexp")) else None,
            "netcash": netcash, "netcash_note": None,
            "rnd": (y["rnd"] / y["rev"] * 100) if (y.get("rnd") is not None and y.get("rev")) else None,
            "dilution": pct(sh, shp), "dil_annual": dil_annual,
            "bvps_chg": pct(bv, bvp),
            "rev1y": pct(y.get("rev"), yp.get("rev")),
            "px1y": pct(p, px_on(ago(end, 365))),
            "dd": (p / px_max(ago(end, 730), end) - 1) * 100 if (p and px_max(ago(end, 730), end)) else None,
            "mcap_fin": mcap, "mcap_raw": mcap,
            "mcap_krw": mcap * fx if (mcap and fx) else None,
            "per": (mcap / y["ni"]) if (mcap and y.get("ni") and y["ni"] > 0) else None,
            "peg": None,                 # 과거 애널 추정치 없음 → PER÷연평균 성장 경로
            "insider": None,             # 지금 값뿐
            "qmargin": [], "qgrowth": [],  # 분기 자료 없음
            "beats": None,
            "dy": dy, "div_yrs": n if n else None,
        }
        memo = {"price": p, "mcap": mcap, "shares_adj": sh}
        out.append((end, d, memo))
    return out


def edgar_year_scores(t):
    """해마다 성장·장기 점수. 지금 채점 함수를 그대로 쓴다.

    돌려주는 값: [{"end", "ten", "lt", "n_ten", "n_lt", "items_ten", "items_lt",
                  "d", "memo"}, ...]  오래된 순
    """
    rows = []
    for end, d, memo in edgar_year_inputs(t):
        try:
            it10, itlt = score_ten(d), score_lt(d)
            _, _, p10 = pctile(it10, TEN_MAX)
            _, _, plt = pctile(itlt, LT_MAX)
        except Exception:
            continue
        rows.append({"end": end, "ten": p10, "lt": plt,
                     "n_ten": sum(1 for x in it10 if x[1] is not None),
                     "n_lt": sum(1 for x in itlt if x[1] is not None),
                     "items_ten": it10, "items_lt": itlt, "d": d, "memo": memo})
    return rows


@cache(1800)
def filings(t, n=10, kinds=None, all_forms=False):
    """최근 공시 목록. 실패하면 빈 리스트.

    kinds      그 서류만 (예: ["8-K", "10-Q"])
    all_forms  True 면 임원 매매까지 전부
    """
    import urllib.request
    cik = _sec_ticker_map().get(str(t).upper().replace("-", "-"))
    if not cik:
        return []
    try:
        req = urllib.request.Request(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers={"User-Agent": SEC_UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except Exception:
        return []

    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accs = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []
    descs = recent.get("primaryDocDescription") or []
    items = recent.get("items") or []

    out = []
    seen = set()                    # 같은 접수번호가 여러 줄로 오는 일이 있다
    for i in range(min(len(forms), len(dates))):
        form = forms[i]
        if kinds and form not in kinds:
            continue

        acc_raw = accs[i] if i < len(accs) else ""
        if acc_raw and acc_raw in seen:
            continue
        if acc_raw:
            seen.add(acc_raw)
        acc = acc_raw.replace("-", "")
        doc = docs[i] if i < len(docs) else ""
        link = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                f"{acc}/{doc}") if acc and doc else ""
        _kind = FILING_KIND.get(form)
        if not _kind:
            _base = form.split("/")[0]
            _kind = FILING_KIND.get(_base)
            if _kind and "/" in form:
                _kind += " 수정본"
        out.append({
            "form": form,
            "date": dates[i],
            "kind": _kind or f"{form} 서류",
            "desc": (descs[i] if i < len(descs) else "") or "",
            "items": (items[i] if i < len(items) else "") or "",
            "link": link,
        })
        if len(out) >= max(n * 6, 60):     # 넉넉히 받아 두고 아래에서 가른다
            break

    # 중요 공시를 앞으로, 임원 매매를 뒤로
    main = [x for x in out if not _is_noise(x["form"])]
    insider = [x for x in out if _is_noise(x["form"])]
    if kinds:
        return out[:n]
    if all_forms:
        # --all: 중요 공시는 그대로 n 건 + 임원 매매는 넉넉히.
        #   (전엔 최근 n 건만 잘라서, 임원 매매가 몰린 주엔 8-K·10-Q 가 통째로 밀려났다)
        return main[:n] + insider[:max(n * 3, 30)]
    # 중요한 것 n 건 + 임원 매매 몇 건
    return main[:n] + insider[:max(0, n - len(main[:n])) or 3]




@cache(86400)
def holders(t, n=5):
    """대주주 정보. 실패하면 빈 dict.

    돌려주는 값
      insider_pct  내부자 보유 %        inst_pct  기관 보유 %
      n_inst       기관 수              top  [{"name","pct","date"}, ...] 상위 n곳
    ★ 기관 주주는 13F(분기 보고) 라 한 달 반쯤 늦다. 한국 종목은 거의 비어 있다.
      야후 버전마다 표 모양·열 이름이 달라서 여러 이름을 찾는다.
    """
    out = {}
    try:
        tk = yf.Ticker(t)
    except Exception:
        return out

    # 내부자·기관 비중
    try:
        mh = tk.major_holders
        if mh is not None and len(mh):
            vals = {}
            if "Value" in getattr(mh, "columns", []):          # 새 형식: index=이름, Value 열
                for k, v in mh["Value"].items():
                    vals[str(k)] = v
            else:                                           # 옛 형식: 0열=값, 1열=설명
                for _, r in mh.iterrows():
                    vals[str(r.iloc[1])] = r.iloc[0]

            def _pct(v):
                try:
                    s = str(v).replace("%", "").strip()
                    x = float(s)
                    return x * 100 if x <= 1.0 and "%" not in str(v) else x
                except Exception:
                    return None
            for k, v in vals.items():
                kl = k.lower()
                if "insider" in kl and "insider_pct" not in out:
                    out["insider_pct"] = _pct(v)
                elif ("institution" in kl and "float" not in kl
                      and "count" not in kl and "number" not in kl
                      and "inst_pct" not in out):
                    out["inst_pct"] = _pct(v)
                elif "count" in kl or "number of institutions" in kl:
                    try:
                        out["n_inst"] = int(float(v))
                    except Exception:
                        pass
    except Exception:
        pass

    # 큰 기관 주주
    try:
        ih = tk.institutional_holders
        if ih is not None and len(ih):
            cols = {c.lower(): c for c in ih.columns}
            c_name = cols.get("holder")
            c_pct = cols.get("pctheld") or cols.get("% out") or cols.get("% held")
            c_date = cols.get("date reported") or cols.get("date")
            top = []
            for _, r in ih.head(n).iterrows():
                name = str(r[c_name]) if c_name else "-"
                pct = None
                if c_pct is not None:
                    try:
                        x = float(str(r[c_pct]).replace("%", ""))
                        pct = x * 100 if x <= 1.0 else x
                    except Exception:
                        pct = None
                dt_ = None
                if c_date is not None:
                    try:
                        dt_ = str(r[c_date])[:10]
                    except Exception:
                        dt_ = None
                top.append({"name": name, "pct": pct, "date": dt_})
            if top:
                out["top"] = top
    except Exception:
        pass
    return out


# 번역기가 막혔을 때 잠깐 쉬게 하는 시각 (같은 프로그램이 도는 동안만 기억)
_TR_REST = {"google": 0.0}


def _tr_chunks(text, limit):
    """문장 단위로 끊어 limit 글자 이하 덩어리로."""
    t = str(text).replace("\n", " ").strip()
    out, cur = [], ""
    for sent in t.split(". "):
        piece = (sent if sent.endswith(".") else sent + ".") + " "
        while len(piece) > limit:                 # 한 문장이 너무 길면 자른다
            out.append(piece[:limit]); piece = piece[limit:]
        if len(cur) + len(piece) > limit and cur:
            out.append(cur); cur = ""
        cur += piece
    if cur.strip():
        out.append(cur)
    return [x.strip() for x in out if x.strip()]


@cache(604800)
def translate_ko(text):
    """영어 → 한국어 자동 번역. 실패하면 None (화면은 영어 원문을 보여 준다).

    ★ 무료 번역기라 막힐 때가 있다.
      1) 구글 번역 — 막히면(요청 과다) 30분 동안 다시 안 부른다
      2) MyMemory — 구글이 안 될 때. 이름 없이 쓰면 하루 5,000자까지
    ★ 한 번 번역한 건 일주일 저장한다.
    """
    import time as _t
    if not text:
        return None
    try:
        import deep_translator as _dt
    except Exception:
        return None

    # 1) 구글
    if _t.time() >= _TR_REST["google"]:
        try:
            gt = _dt.GoogleTranslator(source="auto", target="ko")
            out = [gt.translate(ch) for ch in _tr_chunks(text, 4500)]
            res = " ".join(x for x in out if x)
            if res:
                return res
        except Exception as e:
            if "TooManyRequests" in type(e).__name__ or "too many" in str(e).lower():
                _TR_REST["google"] = _t.time() + 1800

    # 2) MyMemory (한 번에 500자까지)
    try:
        mm = _dt.MyMemoryTranslator(source="en-GB", target="ko-KR")
        out = []
        for ch in _tr_chunks(text, 450):
            r = mm.translate(ch)
            # 한도가 차면 번역문 자리에 경고문을 돌려준다 → 실패로 본다
            if not r or "MYMEMORY WARNING" in r.upper() or "QUERY LENGTH LIMIT" in r.upper():
                return None
            out.append(r)
        res = " ".join(out)
        return res or None
    except Exception:
        return None


@cache(1800)
def news(t, n=6):
    """야후가 주는 최근 뉴스. 실패하면 빈 리스트.

    ★ 호재·악재 판단은 사람이 한다. 우리는 제목과 링크만 옮긴다.
      야후가 주는 항목 이름이 버전마다 달라서 여러 이름을 찾는다.
    """
    try:
        raw = yf.Ticker(t).news or []
    except Exception:
        return []
    out = []
    for it in raw[:n * 2]:
        # 새 형식은 {"content": {...}}, 옛 형식은 평평한 dict
        c = it.get("content") if isinstance(it.get("content"), dict) else it
        title = (c.get("title") or c.get("headline") or "").strip()
        if not title:
            continue
        pub = (c.get("provider") or {})
        src = (pub.get("displayName") if isinstance(pub, dict) else None) \
            or c.get("publisher") or ""
        link = ""
        for k in ("canonicalUrl", "clickThroughUrl", "link"):
            v = c.get(k)
            if isinstance(v, dict):
                v = v.get("url")
            if v:
                link = v
                break
        when = c.get("pubDate") or c.get("displayTime") or c.get("providerPublishTime")
        ago = None
        try:
            if isinstance(when, (int, float)):
                dt_ = datetime.fromtimestamp(when)
            elif isinstance(when, str) and when:
                dt_ = datetime.fromisoformat(when.replace("Z", "+00:00"))
                dt_ = dt_.replace(tzinfo=None)
            else:
                dt_ = None
            if dt_:
                h = (datetime.now() - dt_).total_seconds() / 3600
                ago = ("방금" if h < 1 else f"{h:.0f}시간 전" if h < 24
                       else f"{h/24:.0f}일 전")
        except Exception:
            pass
        out.append({"title": title, "source": src, "link": link, "ago": ago})
        if len(out) >= n:
            break
    return out


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
    if h is None:
        return None
    # ★ chart_data 와 같은 이유. 오늘 행의 Close/Volume 이 NaN 으로 올 수 있다.
    #   그 상태로 계산하면 px_chg 가 nan 이 되고, nan 은 조건식에서 False 로
    #   떨어져 점수가 조용히 틀어진다.
    h = h.dropna(subset=["Close", "Volume"])
    if len(h) < 60:
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
    # 거래가 거의 없는 종목은 20일 평균 거래량이 0 이라 나누기가 터진다.
    # 1,004개로 넓히니 그런 종목이 나왔다.
    _den = float(v20.iloc[-1]) * 20 if v20.iloc[-1] else 0.0
    obv_chg = (float(obv.iloc[-1] - obv.iloc[-21]) / _den) if _den else 0.0
    _c21 = float(c.iloc[-21]) if c.iloc[-21] else 0.0
    px_chg = (float(c.iloc[-1]) / _c21 - 1) if _c21 else 0.0
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
    if h is None or h.empty:
        return None
    # ★ 장중에 받으면 오늘 행이 반쯤 와서 Close 가 NaN 이다.
    #   그대로 두면 c.iloc[-1] 이 NaN 이라 수익률이 전부 nan 으로 나온다.
    h = h.dropna(subset=["Close"])
    if len(h) < 30:
        return None

    c = h["Close"]
    ma20 = c.rolling(20).mean()
    ma50 = c.rolling(50).mean()
    ma120 = c.rolling(120).mean()

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
            "ma120": (float(ma120.iloc[i]) if ma120.iloc[i] == ma120.iloc[i] else None),
        })

    return {
        "rows": rows,
        "ret_7d": ret(5), "ret_1m": ret(21),
        "ret_3m": ret(63), "ret_6m": ret(126), "ret_1y": ret(251),
        "hi": float(c.max()), "lo": float(c.min()),
        "last": float(c.iloc[-1]),
    }



# ═════════════════════════════════════════════════════════════
# 가격 신호
#
#   ★ 화면은 이 표에 있는 규칙만 보여 준다. 없으면 아무것도 안 그린다.
#     원칙은 "sigtest 통과한 것만" 이다. 예외로 켠 것은 "verified": False 로 두고
#     화면에 "검증 미통과 · 참고" 라고 적는다.
#
#   통과 기준 (2026-09-23 에 미리 정해 둔 것)
#     · 대조군 Z(아무 날 매수) 를 연도별로 뺀 값으로 판정
#     · 표본 20개 넘는 연도 중 80% 이상 같은 부호
#     · Z 대비 |3%p| 이상
#     · 21일·63일 둘 다 같은 방향으로 통과
#
#   채우는 법 (sigtest 결과를 그대로 옮긴다)
#     "키": {"label", "dir"(+ 또는 −), "vsz"(Z대비 %p), "years", "hold",
#            "extra"(다른 보유기간 등), "caveat"(주의 문구)}
SIGNALS = {
    # ★ 굥 결정으로 켠다 (2026-09-23). 검증은 통과하지 못했다 — 참고용.
    #   숫자는 전부 굥이 돌린 sigtest 실제 출력 그대로.
    #     나스닥100 (99종목 · 15년)   63일 −0.2%p · 8/15년  탈락   21일 +0.4%p · 10/15년 탈락
    #     1,004종목                 63일 +7.6%p · 12/15년 통과   (중앙값 −4.3)
    #     93종목                    63일 +2.9%p · 11/14년 문턱 미달
    #   → 좋은 회사(나스닥100)에선 아무 날 산 것과 차이가 없었다.
    "E": {"label": "RSI 30 아래",
          "dir": "+",
          "verified": False,
          "vsz": -0.2, "years": "8/15", "hold": 63,
          "extra": "나스닥100 기준 · 21일 +0.4%p (10/15년)",
          "caveat": "검증 미통과 · 참고용. 나스닥100 15년에선 아무 날 산 것과 차이가 "
                    "없었다. 1,004종목(소형주 포함)에선 +7.6%p 였는데, 소형주가 "
                    "튀어 오른 몫으로 보인다"},
    # ★ 굥 결정으로 켠다 (2026-09-23). 아직 sigtest 로 한 번도 재지 않았다.
    #   숫자가 없으므로 화면에 수치를 안 적는다.
    "L": {"label": "RSI 80 위",
          "dir": "−",
          "verified": False, "tested": False,
          "vsz": None, "years": None, "hold": None,
          "caveat": "아직 검증 안 함 · 참고용. 과열 표시일 뿐이고, 강하게 오르는 "
                    "종목은 80 위에서 한참 더 가기도 한다"},
    # ★ 굥 결정으로 켠다 (2026-09-23). 아직 재지 않았다. 사라/팔아라가 아니라 표시만.
    "M": {"label": "RSI 50 상향 돌파",
          "dir": "0", "shape": "dot",
          "min_gap": 10,        # 50 근처에서 오르내리면 며칠 새 여러 번 떠서 점이 겹친다
          "verified": False, "tested": False,
          "vsz": None, "years": None, "hold": None,
          "caveat": "아직 검증 안 함 · 참고용. RSI 가 50 을 아래에서 위로 넘은 날 "
                    "(힘이 약한 쪽에서 강한 쪽으로 넘어간 날) 표시"},
}


def _rsi_list(c, n=14):
    """Wilder RSI. sigtest.py 와 같은 식이어야 한다.
    (pandas ewm(alpha=1/n, adjust=False) 를 손으로 푼 것)"""
    out = [None] * len(c)
    if len(c) < n + 2:
        return out
    a = 1.0 / n
    au = ad = None
    for i in range(1, len(c)):
        d = c[i] - c[i - 1]
        g, l = (d if d > 0 else 0.0), (-d if d < 0 else 0.0)
        if au is None:
            au, ad = g, l
        else:
            au = (1 - a) * au + a * g
            ad = (1 - a) * ad + a * l
        if ad == 0:
            out[i] = None
        else:
            out[i] = 100 - 100 / (1 + au / ad)
    return out


# 규칙이 "상태"인지 "사건"인지.
#   상태(state)  = 며칠씩 이어진다 → 차트엔 켜지는 첫날만 찍는다
#   사건(event)  = 그날 하루 → 그날 찍는다
RULE_KIND = {"B": "state", "C": "event", "E": "state", "G": "state",
             "I": "event", "J": "event", "L": "state", "M": "event"}


def _rule_series(rows):
    """SIGNALS 에 있는 규칙의 날짜별 참/거짓.

    돌려주는 값: {"B": [None, None, True, ...], ...}
    계산 못 한 날은 None.
    """
    if not SIGNALS or not rows:
        return {}
    c = [r["close"] for r in rows]
    v = [r.get("volume") or 0.0 for r in rows]
    o = [r.get("open") or r["close"] for r in rows]
    n = len(c)

    def sma(xs, k, i):
        if i < 0 or i + 1 < k:
            return None
        return sum(xs[i + 1 - k:i + 1]) / k

    rsi = _rsi_list(c) if any(k in SIGNALS for k in ("E", "L", "M")) else None

    out = {}
    for key in SIGNALS:
        ser = [None] * n
        if key in ("E", "L", "M"):
            # RSI 는 앞 50일쯤은 아직 안정되지 않아 쓰지 않는다
            for i in range(n):
                if i < 51 or rsi[i] is None:
                    continue
                if key == "E":
                    ser[i] = rsi[i] < 30
                elif key == "L":
                    ser[i] = rsi[i] > 80
                else:                               # M: 50 을 아래에서 위로
                    ser[i] = (rsi[i - 1] is not None and rsi[i - 1] <= 50
                              and rsi[i] > 50)
            out[key] = ser
            continue
        for i in range(n):
            try:
                if key == "B":
                    m, mp = sma(c, 120, i), sma(c, 120, i - 5)
                    ser[i] = (c[i] > m and m > mp) if (m and mp) else None
                elif key == "C":
                    if i >= 1:
                        win = c[max(0, i - 252):i]
                        ser[i] = c[i] >= max(win) if win else None
                elif key == "G":
                    win = c[max(0, i - 251):i + 1]
                    hi = max(win) if win else None
                    if hi:
                        dd = (c[i] / hi - 1) * 100
                        ser[i] = -45 <= dd <= -25
                elif key == "I":
                    a1, a2 = sma(c, 20, i), sma(c, 50, i)
                    b1, b2 = sma(c, 20, i - 1), sma(c, 50, i - 1)
                    if None not in (a1, a2, b1, b2):
                        ser[i] = a1 > a2 and b1 <= b2
                elif key == "J":
                    v20 = sma(v, 20, i)
                    if v20:
                        ser[i] = v[i] > 2 * v20 and c[i] > o[i]
            except Exception:
                ser[i] = None
        out[key] = ser
    return out


def signal_marks(rows, max_each=40):
    """차트에 찍을 화살표.

    돌려주는 값: [{"i": 행번호, "key", "dir", "label"}, ...]
      dir "+" = 사도 됨 쪽 · "−" = 사지 마라 쪽

    ★ 상태형 규칙은 켜지는 첫날만 찍는다.
      매일 찍으면 화면이 화살표로 덮인다.
    ★ 지나간 신호를 보면 "그때 샀으면" 하는 착각이 생긴다.
      그래서 화면에 검증 수치를 같이 적는다.
    """
    ser = _rule_series(rows)
    marks = []
    for key, vals in ser.items():
        meta = SIGNALS.get(key, {})
        kind = RULE_KIND.get(key, "event")
        gap = meta.get("min_gap", 0)           # 같은 표시 사이 최소 간격(거래일)
        idxs = []
        for i, x in enumerate(vals):
            if not x:
                continue
            if kind == "state" and i > 0 and vals[i - 1]:
                continue                      # 이어지는 날은 건너뛴다
            if gap and idxs and i - idxs[-1] < gap:
                continue                      # 너무 붙어 있으면 건너뛴다 (겹쳐 보임)
            idxs.append(i)
        if len(idxs) > max_each:               # 너무 많으면 최근 것만
            idxs = idxs[-max_each:]
        for i in idxs:
            marks.append({"i": i, "key": key,
                          "dir": meta.get("dir", "+"),
                          "verified": meta.get("verified", True),
                          "tested": meta.get("tested", True),
                          "shape": meta.get("shape", "arrow"),
                          "label": meta.get("label", key)})
    return sorted(marks, key=lambda m: m["i"])


def signal_state(rows):
    """차트 데이터로 지금 켜져 있는 신호를 찾는다.

    rows  = chart_data()["rows"]
    돌려주는 값: [{"key","label","on","dir","vsz","years","note"}, ...]

    ★ SIGNALS 에 없는 규칙은 계산도 안 한다.
      검증 안 된 것을 화면에 내지 않기 위해서다.
    ★ A(모멘텀 상위 20%)는 종목끼리 비교해야 해서 한 종목만으로는 못 낸다.
      SIGNALS 에 A 가 들어오면 snapshot 을 봐야 한다.
    """
    if not SIGNALS or not rows or len(rows) < 130:
        return []

    c = [r["close"] for r in rows]
    v = [r.get("volume") or 0.0 for r in rows]
    o = [r.get("open") or r["close"] for r in rows]

    def sma(xs, n, i):
        if i + 1 < n:
            return None
        seg = xs[i + 1 - n:i + 1]
        return sum(seg) / n

    i = len(c) - 1
    out = []
    for k, meta in SIGNALS.items():
        on, note = None, ""
        try:
            if k == "B":
                m120, m120p = sma(c, 120, i), sma(c, 120, i - 5)
                if m120 and m120p:
                    on = c[i] > m120 and m120 > m120p
                    note = f"종가 {c[i]:,.2f} · 120일선 {m120:,.2f}"
            elif k == "C":
                win = c[max(0, i - 252):i]          # 어제까지의 52주 고점
                if win:
                    hi = max(win)
                    on = c[i] >= hi
                    note = f"52주 고점 {hi:,.2f}"
            elif k in ("E", "L", "M"):
                rl = _rsi_list(c)
                r_, rp = rl[i], rl[i - 1]
                if r_ is not None:
                    if k == "E":
                        on = r_ < 30
                    elif k == "L":
                        on = r_ > 80
                    else:
                        on = rp is not None and rp <= 50 < r_
                    note = f"RSI {r_:.0f}"
            elif k == "G":
                win = c[max(0, i - 251):i + 1]
                hi = max(win)
                dd = (c[i] / hi - 1) * 100 if hi else None
                if dd is not None:
                    on = -45 <= dd <= -25
                    note = f"고점 대비 {dd:.0f}%"
            elif k == "I":
                a1, a2 = sma(c, 20, i), sma(c, 50, i)
                b1, b2 = sma(c, 20, i - 1), sma(c, 50, i - 1)
                if None not in (a1, a2, b1, b2):
                    on = a1 > a2 and b1 <= b2
                    note = "20일선이 50일선을 막 넘음" if on else ""
            elif k == "J":
                v20 = sma(v, 20, i)
                if v20:
                    on = v[i] > 2 * v20 and c[i] > o[i]
                    note = f"거래량 평균의 {v[i]/v20:.1f}배"
            else:
                continue                             # 아직 화면용 계산이 없는 규칙
        except Exception:
            on = None
        if on is None:
            continue
        out.append({"key": k, "label": meta.get("label", k), "on": bool(on),
                    "dir": meta.get("dir", "+"), "vsz": meta.get("vsz"),
                    "years": meta.get("years"), "hold": meta.get("hold"),
                    "extra": meta.get("extra"), "caveat": meta.get("caveat"),
                    "verified": meta.get("verified", True),
                    "tested": meta.get("tested", True),
                    "note": note})
    return out


def fp_verdict(score):
    if score >= 70:
        return "매수 우위", ORANGE
    if score <= 40:
        return "매도 우위", "#2563EB"
    return "중립", MUTED



# ═════════════════════════════════════════════════════════════
# 레이더 차트 (5축) — 세부 항목을 5개 축으로 묶어서 SVG로 그림
# ═════════════════════════════════════════════════════════════

AXES_TEN = [
    ("성장성",   ["성장 가속도", "이익률 추세(분기)", "어닝 서프라이즈",
                 "실적-주가 괴리"]),
    ("수익성",   ["영업이익률", "ROE", "이익의 질", "잉여현금흐름"]),
    ("재무안전", ["부채비율", "순현금/시총", "주식수 희석"]),
    ("주가매력", ["밸류에이션", "고점 대비 위치"]),
    ("규모 여력", ["시가총액", "내부자지분", "R&D 집중도"]),
]

AXES_LT = [
    ("현금창출", ["FCF 안정성"]),
    ("안정성",   ["이익률 안정성", "침체 생존력"]),
    ("자본효율", ["자본수익률"]),
    ("성장성",   ["장기 성장률"]),
    ("주주환원", ["주식수 관리", "배당", "부채 안전성", "밸류에이션"]),
]
