"""
투자 스크리너 폰앱 (app.py)
============================
Streamlit Cloud에 올려서 폰 브라우저로 쓰는 버전.

로컬 테스트:
    py -m pip install streamlit yfinance pandas
    py -m streamlit run app.py

배포:
    GitHub에 app.py + requirements.txt 올리고
    share.streamlit.io 에서 Deploy

[구조] 수집·채점 로직을 core.py로 분리 (cli.py와 공용)

[변경] 레이더 차트 추가 (상세화면 5축 오각형)

[변경] 수급 동향 추가
    - 6개월 가격/거래량으로 매수/매도 우위 점수(0~100) 산출
    - 대시보드 카드 한 줄 + 상세 섹션 + 70↑ 배너

[변경] 진입밴드 기능 추가
    - 관심종목 탭에서 종목별 진입밴드 입력 (예: 225-250, 단일값 158도 가능)
    - 대시보드 카드에 밴드 대비 현재가 상태 표시
    - 밴드 안에 들어온 종목은 대시보드 상단에 주황 배너 알림
"""

import json
import os
from datetime import datetime, timedelta

import streamlit as st

from core import (TEN_MAX, LT_MAX, DAMO_MAX, AXES_TEN, AXES_LT,
                  score_damo, damo_verdict, roic_gap_text, USD_KRW, chart_data,
                  money,
                  CHARCOAL, ORANGE, AMBER,
                  score_ten, score_lt, fetch, won, pctile,
                  ten_verdict, lt_verdict, dday, footprint, fp_verdict,
                  macro, vix_mood, implied_growth, fair_range)

# ─────────────────────────────────────────────────────────────
WATCHFILE = "watchlist.json"

st.set_page_config(page_title="스크리너", page_icon="◆", layout="centered")

st.markdown(f"""<style>
#MainMenu, footer, header {{visibility:hidden;}}
.block-container {{padding:1rem 1rem 3rem;max-width:720px;}}
h1,h2,h3 {{color:{CHARCOAL};font-weight:700;}}
.card {{background:#fff;border:1px solid #E4E4E7;border-radius:10px;
       padding:14px 16px;margin-bottom:10px;}}
.card-hd {{display:flex;justify-content:space-between;align-items:baseline;}}
.tkr {{font-size:1.05rem;font-weight:700;color:{CHARCOAL};}}
.nm {{font-size:.72rem;color:#9CA3AF;}}
.px {{font-size:1.05rem;font-weight:700;color:{CHARCOAL};}}
.up {{color:#DC2626;font-size:.8rem;font-weight:600;}}
.dn {{color:#2563EB;font-size:.8rem;font-weight:600;}}
.badge {{display:inline-block;padding:2px 9px;border-radius:4px;
        font-size:.7rem;font-weight:700;color:#fff;}}
.bar-bg {{height:5px;background:#F1F1F2;border-radius:3px;margin-top:9px;}}
.bar-fl {{height:5px;border-radius:3px;}}
.dday {{background:{CHARCOAL};color:#fff;padding:9px 14px;border-radius:8px;
       margin-bottom:7px;font-size:.82rem;}}
.entry {{background:{ORANGE};color:#fff;padding:9px 14px;border-radius:8px;
        margin-bottom:7px;font-size:.82rem;}}
.bandline {{display:flex;justify-content:space-between;margin-top:8px;
           font-size:.72rem;}}
.metric {{display:flex;justify-content:space-between;padding:7px 0;
         border-bottom:1px solid #F1F1F2;font-size:.85rem;}}
.mk {{color:#6B7280;}} .mv {{color:{CHARCOAL};font-weight:600;}}
.sect {{font-size:.7rem;letter-spacing:.1em;color:{ORANGE};
       font-weight:700;margin:18px 0 7px;}}
.note {{font-size:.72rem;color:#9CA3AF;line-height:1.5;}}
.stButton>button {{border-radius:7px;font-weight:600;}}

/* ───────── 2026-09-13 디자인 개편 ─────────
   "뭘 봐야 할지 모르겠다" 를 없애는 게 목적이다.
   숫자를 더 넣지 않고, 이미 있는 숫자에 위계를 준다.     */

/* 번호 붙은 질문형 섹션 제목 — 읽는 순서를 눈으로 알려 준다 */
.qhd {{display:flex;align-items:center;gap:9px;
      margin:26px 0 10px;padding-bottom:7px;
      border-bottom:2px solid #EDEDF0;}}
.qno {{flex:none;width:21px;height:21px;border-radius:50%;
      background:{CHARCOAL};color:#fff;font-size:.68rem;font-weight:700;
      display:flex;align-items:center;justify-content:center;}}
.qtx {{font-size:1rem;font-weight:700;color:{CHARCOAL};letter-spacing:-.01em;}}
.qsub {{font-size:.68rem;color:#9CA3AF;font-weight:400;margin-left:auto;}}
/* 참고용 섹션은 번호를 회색으로 — 비중이 낮다는 걸 색으로 말한다 */
.qno.ref {{background:#C7CAD1;}}

/* 맨 위 요약 카드 */
.sum {{background:#fff;border:1px solid #E4E4E7;border-radius:12px;
      padding:15px 16px 13px;margin:2px 0 6px;}}
.sumg {{display:grid;grid-template-columns:1fr 1fr;gap:11px 8px;}}
.sumc {{text-align:center;padding:9px 4px;border-radius:8px;background:#FAFAFB;}}
.sumk {{font-size:.66rem;color:#9CA3AF;letter-spacing:.04em;margin-bottom:3px;}}
.sumv {{font-size:1.5rem;font-weight:800;line-height:1.1;}}
.suml {{font-size:.7rem;font-weight:700;margin-top:2px;}}
.sumsay {{margin-top:12px;padding:11px 13px;border-radius:8px;
         background:#F7F7F8;border-left:3px solid {ORANGE};
         font-size:.84rem;line-height:1.6;color:{CHARCOAL};}}

/* 숫자를 키운다 — 핵심 % 는 멀리서도 읽히게 */
.bigpct {{font-size:1.45rem;font-weight:800;line-height:1;}}
.bigsub {{font-size:.7rem;color:#9CA3AF;font-weight:500;margin-left:5px;}}

/* 참고용 블록은 통째로 눌러 놓는다 (수급처럼 예측력 없는 것) */
.dim {{opacity:.82;}}
.refbox {{background:#FAFAFB;border:1px dashed #E0E0E5;border-radius:9px;
         padding:11px 13px;font-size:.75rem;color:#6B7280;line-height:1.55;}}

/* 차트 아래 추세 한 줄 */
.trendline {{background:#FAFAFB;border:1px solid #EDEDF0;border-radius:7px;
            padding:7px 11px;font-size:.78rem;color:{CHARCOAL};
            margin-bottom:6px;}}

/* 대시보드 카드의 점수 줄 — 성장·장기·가격을 한눈에 */
.srow {{display:flex;align-items:center;gap:8px;margin-top:6px;}}
.sname {{flex:none;width:30px;font-size:.7rem;color:#9CA3AF;}}
.snum {{flex:none;width:26px;font-size:.82rem;font-weight:800;text-align:right;}}
.sbar {{flex:1;height:5px;background:#F1F1F2;border-radius:3px;overflow:hidden;}}
.sfil {{height:5px;border-radius:3px;}}
.stag {{flex:none;width:58px;font-size:.66rem;font-weight:700;text-align:right;}}
.cfoot {{display:flex;justify-content:space-between;margin-top:9px;
        padding-top:7px;border-top:1px solid #F1F1F2;font-size:.68rem;
        color:#9CA3AF;}}
.rankno {{display:inline-block;min-width:17px;height:17px;line-height:17px;
         border-radius:4px;background:#F1F1F2;color:#6B7280;font-size:.62rem;
         font-weight:700;text-align:center;margin-right:5px;}}

/* 맨 위 시장 상황 바 */
.macro {{display:flex;gap:8px;margin:0 0 8px;}}
.mcell {{flex:1;background:#fff;border:1px solid #E4E4E7;border-radius:9px;
        padding:7px 10px;display:flex;align-items:baseline;gap:5px;}}
.mk2 {{font-size:.66rem;color:#9CA3AF;}}
.mv2 {{font-size:1rem;font-weight:800;}}
.ml2 {{font-size:.66rem;font-weight:700;margin-left:auto;}}
.mnote {{font-size:.66rem;color:#9CA3AF;margin:-4px 0 8px;line-height:1.5;}}

/* ③ 가격 판정 — 요구 성장률을 제일 크게 */
.reqbox {{background:{CHARCOAL};color:#fff;border-radius:11px;
         padding:14px 16px;margin:4px 0 10px;}}
.reqk {{font-size:.66rem;letter-spacing:.06em;opacity:.7;margin-bottom:3px;}}
.reqv {{font-size:1.6rem;font-weight:800;line-height:1.15;}}
.reqsub {{font-size:.72rem;opacity:.72;line-height:1.5;margin-top:5px;}}
.reqcmp {{margin-top:9px;padding-top:9px;border-top:1px solid rgba(255,255,255,.18);
         font-size:.8rem;line-height:1.5;}}
.fbox {{background:#fff;border:1px solid #E4E4E7;border-radius:11px;
       padding:13px 15px;margin-bottom:10px;}}
.fchips {{font-size:.78rem;color:{CHARCOAL};font-weight:600;margin-bottom:9px;}}
.fbar {{position:relative;height:6px;border-radius:3px;margin:10px 0 3px;
       background:linear-gradient(90deg,#FDE8D4,#F1F1F2,#DCE6F7);}}
.fdot {{position:absolute;top:-3px;width:12px;height:12px;margin-left:-6px;
       border-radius:50%;background:{CHARCOAL};border:2px solid #fff;
       box-shadow:0 0 0 1px {CHARCOAL};}}
.fends {{display:flex;justify-content:space-between;font-size:.66rem;
        color:#9CA3AF;margin-bottom:8px;}}
.fratio {{font-size:.84rem;line-height:1.5;color:{CHARCOAL};}}
.fwarn {{margin-top:9px;padding:9px 11px;border-radius:7px;
        background:#FFF4F4;border-left:3px solid #DC2626;
        font-size:.75rem;color:#991B1B;line-height:1.55;}}
.buybox {{padding:10px 12px;border-radius:8px;background:#F7F7F8;
         border-left:3px solid {ORANGE};font-size:.86rem;line-height:1.55;
         margin-bottom:8px;}}

/* 다모다란 옆에 다른 점수를 같이 띄우는 줄 */
.xref {{display:flex;gap:7px;flex-wrap:wrap;margin:8px 0 2px;}}
.xchip {{font-size:.7rem;padding:3px 9px;border-radius:99px;
        background:#F1F1F2;color:#6B7280;font-weight:600;}}
</style>""", unsafe_allow_html=True)


# ═════════════════════════════════════════════════════════════
# 비밀번호
# ═════════════════════════════════════════════════════════════

def gate():
    # secrets.toml 이 아예 없으면 st.secrets 접근 자체가 예외를 던진다.
    # 로컬 PC 테스트에서는 그냥 통과시키고, 클라우드에서만 비밀번호를 묻는다.
    try:
        pw = st.secrets.get("password")
    except Exception:
        pw = None
    if not pw:
        return True                      # 비번 미설정 시 통과 (로컬 테스트용)
    if st.session_state.get("ok"):
        return True
    st.markdown("### ◆ 스크리너")
    v = st.text_input("비밀번호", type="password", label_visibility="collapsed",
                      placeholder="비밀번호")
    if v:
        if v == pw:
            st.session_state["ok"] = True
            st.rerun()
        else:
            st.error("비밀번호가 맞지 않습니다.")
    return False


# ═════════════════════════════════════════════════════════════
# 관심종목 + 진입밴드 (watchlist.json 하나에 같이 저장)
#   구버전(리스트만 있던 파일)도 자동으로 읽어서 변환함
# ═════════════════════════════════════════════════════════════

def load_state():
    if "wstate" in st.session_state:
        return st.session_state["wstate"]
    try:
        with open(WATCHFILE, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, list):                       # 구버전 호환
            stt = {"tickers": raw, "bands": {}}
        else:
            # 모양이 깨진 밴드는 여기서 걸러 낸다. 안 그러면 다음 실행에서
            # band_status 가 터져 앱이 안 열린다. (2026-09-13)
            stt = {"tickers": raw.get("tickers", []),
                   "bands": clean_bands(raw.get("bands"))}
    except Exception:
        stt = {"tickers": [], "bands": {}}
    st.session_state["wstate"] = stt
    return stt


def save_state(stt):
    st.session_state["wstate"] = stt
    try:
        # 임시 파일에 쓴 뒤 바꿔치기 — 쓰다 끊겨도 기존 목록이 살아남는다
        tmp = WATCHFILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stt, f, ensure_ascii=False)
        os.replace(tmp, WATCHFILE)
    except Exception as e:
        # 조용히 넘기면 저장된 줄 알고 있다가 세션이 끝날 때 잃는다.
        # 클라우드에서는 원래 재배포 때 날아가므로 백업을 권한다. (2026-09-13)
        st.warning(f"관심종목을 파일에 저장하지 못했습니다 ({e}). "
                   f"아래 백업 문자열을 복사해 두세요.")


def _px(d):
    """현재가 표기. 원화는 소수점 없이, 그 외는 두 자리.

    ★ 2026-09-13 — 예전에는 f"{p:,.2f}" 로 고정이라
      삼성전자가 '71,500.00' 으로 나왔다. cli.py 와 같은 규칙이다.
    """
    p = d.get("price")
    if not p:
        return "-"
    return f"{p:,.0f}" if d.get("currency") == "KRW" else f"{p:,.2f}"


def parse_band(s):
    """'225-250' / '225~250' / '158' → [lo, hi], 실패 시 None

    ★ 2026-09-13 — 예전 방식은 split('-') 으로 빈 조각을 버려서
      '-5' 가 [5, 5] 가 되고 '225-250-300' 이 [225, 250] 으로
      조용히 바뀌었다. 0 도 그대로 통과해 band_status 에서
      ZeroDivisionError 를 냈고, 그 값이 watchlist.json 에 저장돼
      앱이 아예 안 열리는 상태가 됐다.
      이제 형식에 정확히 맞는 양수만 받는다.
    """
    import re as _re
    s = (s or "").replace("~", "-").replace(",", "").strip()
    if not s:
        return None
    m = _re.fullmatch(r"(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?", s)
    if not m:
        return None
    try:
        lo = float(m.group(1))
        hi = float(m.group(2)) if m.group(2) else lo
    except Exception:
        return None
    if lo <= 0 or hi <= 0:          # 0 이나 음수는 밴드가 될 수 없다
        return None
    return [min(lo, hi), max(lo, hi)]


def valid_band(b):
    """저장된 밴드가 쓸 수 있는 값인지. 백업 복원으로 들어온 값 방어용."""
    return (isinstance(b, (list, tuple)) and len(b) == 2
            and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                    and x > 0 for x in b))


def clean_bands(raw):
    """백업에서 온 bands 를 걸러 낸다.

    ★ 복원 칸에 손으로 고친 백업을 붙여넣는 일이 잦은데
      {"MU": 250} 처럼 모양이 다르면 다음 실행에서 앱이 통째로 죽었다.
      쓸 수 있는 것만 남긴다.
    """
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if valid_band(v):
                out[str(k)] = [float(v[0]), float(v[1])]
    return out


def band_text(band):
    if not valid_band(band):
        return ""
    lo, hi = band
    f = lambda v: f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"
    return f(lo) if lo == hi else f"{f(lo)}~{f(hi)}"


def band_status(price, band):
    """(라벨, 색, 밴드 안 여부) — 밴드 미설정/가격 없음이면 None"""
    # ★ 2026-09-13 — lo/hi 로 나누는데 0 이나 잘못된 모양을 거르지 않아
    #   ZeroDivisionError / TypeError 로 앱 전체가 죽었다.
    #   그 값이 파일에 먼저 저장되므로 재시작해도 안 살아났다.
    if not price or not valid_band(band):
        return None
    lo, hi = band
    if lo <= price <= hi:
        return "◆ 밴드 안", ORANGE, True
    if price > hi:
        d = (price / hi - 1) * 100
        if d <= 5:
            return f"밴드까지 -{d:.1f}%", AMBER, False
        return f"밴드 위 +{d:.1f}%", "#6B7280", False
    d = (1 - price / lo) * 100
    return f"밴드 하회 -{d:.1f}%", AMBER, False


# ═════════════════════════════════════════════════════════════
# 화면
# ═════════════════════════════════════════════════════════════



def fp_line(fp):
    """대시보드 카드용 한 줄."""
    if not fp:
        return ""
    lab, col = fp_verdict(fp["score"])
    return (f'<div class="bandline"><span style="color:#9CA3AF">수급 동향</span>'
            f'<span style="color:{col};font-weight:700">{fp["score"]} · {lab}</span></div>')



# ═════════════════════════════════════════════════════════════
# 주가 차트 (SVG, 외부 라이브러리 없이)
# ═════════════════════════════════════════════════════════════


# ═════════════════════════════════════════════════════════════
# 다모다란 관점 (채점과 무관, 참고용)
#   지금 채점은 "이미 잘하고 있는 회사"를 찾는 잣대라
#   적자인 초기 성장주가 크게 깎인다.
#   다모다란은 "앞으로 얼마나 벌지"를 보므로 관점이 다르다.
#   섞으면 6개월 실험 비교가 깨지므로 점수에는 넣지 않는다.
# ═════════════════════════════════════════════════════════════

def damo_section(d):
    rc = d.get("roic")
    re_ = d.get("reinv_eff")
    revs = d.get("revs") or []
    margins = d.get("margins") or []
    if rc is None and re_ is None and len(revs) < 2:
        return

    cur = d.get("fin_currency") or d.get("currency", "USD")
    GREEN, RED, GRAY = "#16A34A", "#DC2626", "#6B7280"

    # ── 점수 (기존 채점과 별개) ──
    # ★ 2026-09-13 — 예전에는 여기서 score_damo(d) 를 자본비용 기본값(9%)
    #   으로만 매겼다. 아래 '세부 보기' 의 슬라이더를 12% 로 올려도
    #   위 점수는 9% 그대로여서, 같은 화면에서 초과수익이 (-) 인데
    #   ROIC 초과수익 점수는 만점인 모순이 보였다.
    #   슬라이더 값은 st.session_state 에 남으므로 그걸 먼저 읽어
    #   점수와 세부 보기가 같은 가정을 쓰게 한다.
    wacc = st.session_state.get(f"wacc_{d['ticker']}")
    items = score_damo(d, wacc)
    if items:
        got, avail, pct_ = pctile(items, DAMO_MAX)
        lab, col_ = damo_verdict(pct_)
        st.markdown(f"""<div style="display:flex;justify-content:space-between;
          align-items:center;margin-bottom:6px">
          <span class="badge" style="background:{col_}">{lab}</span>
          <span class="bigpct" style="color:{col_}">{pct_:.0f}<span
            style="font-size:.85rem">%</span>
            <span class="bigsub">{got}/{avail}</span></span></div>
          <div class="bar-bg"><div class="bar-fl"
          style="width:{pct_}%;background:{col_}"></div></div>""",
          unsafe_allow_html=True)

        # ★ 2026-09-13 — 다른 두 점수를 여기 같이 띄운다.
        #   CRDO 는 이 점수가 100 점인데 성장은 상위 43%, 장기는 상위 57% 다.
        #   이 화면만 보면 "완벽한 회사" 로 읽힌다. 실제로 그렇게 읽은
        #   사례가 있었다. 같은 자리에 놓아야 엇갈린다는 걸 알 수 있다.
        chips = []
        _ti, _li = score_ten(d), score_lt(d)
        if _ti:
            chips.append(f"성장 잠재력 {pctile(_ti, TEN_MAX)[2]:.0f}%")
        if _li:
            chips.append(f"장기 보유 {pctile(_li, LT_MAX)[2]:.0f}%")
        _fp = footprint(d["ticker"])
        if _fp:
            chips.append(f"수급 {_fp['score']}")
        if chips:
            st.markdown('<div class="xref">'
                        + "".join(f'<span class="xchip">{c}</span>' for c in chips)
                        + '</div>', unsafe_allow_html=True)
            if pct_ >= 75:
                others = [p for p in (pctile(_ti, TEN_MAX)[2] if _ti else None,
                                      pctile(_li, LT_MAX)[2] if _li else None)
                          if p is not None]
                if others and max(others) < 60:
                    st.markdown(
                        '<div class="refbox" style="border-color:#F0C9A0;'
                        'background:#FFF9F2;color:#8A5A2B">'
                        '자본 효율은 최상위인데 성장·장기 점수는 평범합니다. '
                        '“좋은 회사”가 곧 “지금 사도 되는 주식”은 아닙니다. '
                        '아래 ③번(가격)을 꼭 같이 보세요.</div>',
                        unsafe_allow_html=True)
        # 5개 항목이 그대로 5개 축이 된다
        axes_damo = [(k, [k]) for k in DAMO_MAX]
        st.markdown(radar_svg(items, DAMO_MAX, None, pct_, col_, axes_damo),
                    unsafe_allow_html=True)
        st.markdown("".join(
            f'<div class="metric"><span class="mk">{k}</span>'
            f'<span class="mv">{txt} '
            f'<span style="color:#9CA3AF;font-weight:400">'
            f'{sc}/{DAMO_MAX[k]}</span></span></div>'
            for k, sc, txt in items), unsafe_allow_html=True)
        st.markdown('<p class="note">기존 채점(성장 잠재력·장기 보유)과 '
                    '별개입니다. "지금 재무가 좋은가" 가 아니라 '
                    '"자본을 굴려 가치를 만들고 있는가" 를 봅니다.</p>',
                    unsafe_allow_html=True)
    else:
        st.markdown('<p class="note">채점할 데이터가 모자랍니다.</p>',
                    unsafe_allow_html=True)

    with st.expander("세부 보기"):
        wacc = st.slider("자본비용 가정 (%)", 5.0, 15.0, 9.0, 0.5,
                         key=f"wacc_{d['ticker']}",
                         help="보통 8~10%. 위험한 회사일수록 높게 잡는다. "
                              "위 다모다란 점수도 이 값을 따라 다시 매겨진다")

        blocks = []

        # 1. 가치 창출
        rows = []
        note1 = ("ROE 는 빚을 많이 쓰면 부풀려지지만 ROIC 는 그렇지 않습니다. "
                 "자본비용보다 높아야 가치를 만드는 것입니다.")
        if rc is not None:
            gap = rc - wacc
            col = GREEN if gap > 0 else RED
            rows += [("ROIC", f"{rc:.1f}%", None),
                     ("자본비용 가정", f"{wacc:.1f}%", None),
                     ("초과수익",
                      f'<span style="color:{col};font-weight:700">{gap:+.1f}%p</span>'
                      f' · {"가치 창출" if gap > 0 else "가치 파괴"}', None)]

            # ── R&D 자본화 조정 (점수에는 안 들어감) ──
            # 회계는 R&D 를 그해 비용으로 턴다. 그러면 연구로 쌓은 것이
            # 자산에 안 잡혀 투입자본이 작아지고 ROIC 가 높게 나온다.
            ra = d.get("roic_adj")
            if ra is not None:
                g2 = ra - wacc
                c2 = GREEN if g2 > 0 else RED
                life = d.get("rnd_life")
                rows.append(("R&D 자본화 ROIC",
                             f'{ra:.1f}%'
                             + (f'  <span style="color:#9CA3AF">({life}년 상각)</span>'
                                if life else ""), None))
                rows.append(("조정 후 초과수익",
                             f'<span style="color:{c2};font-weight:700">{g2:+.1f}%p</span>'
                             f' · {"가치 창출" if g2 > 0 else "가치 파괴"}', None))
                if (rc >= wacc) != (ra >= wacc):
                    rows.append(("⚠ 판정 뒤집힘",
                                 '<span style="color:#DC2626;font-weight:700">'
                                 'R&D 비중이 커서 기존 ROIC 가 부풀려져 있었습니다'
                                 '</span>', None))
                note1 += ("<br><br>회계는 R&D 를 그해 비용으로 털어냅니다. "
                          "그러면 연구로 쌓아 올린 것이 자산에 안 잡혀 "
                          "투입자본이 작아지고 ROIC 가 실제보다 높게 나옵니다. "
                          "다모다란은 R&D 를 설비투자처럼 자본화하라고 합니다. "
                          "아래가 그렇게 다시 계산한 값입니다. "
                          "점수에는 들어가지 않습니다.")
            elif d.get("roic_adj_note"):
                rows.append(("R&D 자본화 ROIC",
                             f"계산 불가 ({d['roic_adj_note']})", GRAY))
        else:
            rows.append(("ROIC", "계산 불가 (데이터 부족)", GRAY))
        blocks.append(("1. 가치를 만들고 있나", rows, note1))

        # 2. 성장의 대가
        rows = []
        if re_ is not None:
            j = ("효율 높음" if re_ >= 2 else "보통" if re_ >= 0.5 else
                 "투자 회수 전" if re_ >= 0 else "매출 감소 중")
            rows.append(("재투자 효율", f"{re_:.2f}배 · {j}", None))
        cl, cc = d.get("capex_last"), d.get("capex_chg")
        if cl:
            note = f"  (전년比 {cc:+.0f}%)" if cc is not None else ""
            rows.append(("설비투자", money(cl, cur) + note, None))
        fl = d.get("fcf_last")
        if fl is not None:
            extra = " · 성장에 다 쓰는 중" if fl < 0 else ""
            rows.append(("잉여현금흐름", money(fl, cur) + extra, None))
        if rows:
            blocks.append(("2. 성장에 얼마를 쓰고 있나", rows,
                           "설비투자와 R&D 를 합친 돈 1 이 매출을 얼마나 늘렸는지 봅니다. "
                           "적자 회사도 계산되므로 초기 성장주를 볼 수 있습니다."))

        # 3. 성장의 모양
        rows = []
        if len(revs) >= 2:
            gs = [(revs[i+1]/revs[i]-1)*100 for i in range(len(revs)-1)
                  if revs[i] > 0]
            if gs:
                rows.append(("매출 성장률",
                             " → ".join(f"{g:+.0f}%" for g in gs[-4:]), None))
        c = d.get("cagr")
        if c is not None:
            rows.append(("장기 CAGR", f"{c:.1f}%", None))
        if len(margins) >= 2:
            rows.append(("영업이익률",
                         " → ".join(f"{x:.0f}%" for x in margins[-4:]), None))
            # 추세 계산식은 core.py 의 score_damo 와 똑같이 맞춘다.
            # 예전에는 여기서 margins[-1] - margins[0] 을 썼는데,
            # 점수 항목은 앞뒤 2년 평균 차이를 써서 같은 화면에
            # +6%p 와 +8%p 가 동시에 나왔다. (ANET 2026-09-13)
            # 한 해가 튀는 것에 덜 휘둘리는 쪽(점수 방식)으로 통일했다.
            if len(margins) >= 3:
                late = sum(margins[-2:]) / 2
                early = sum(margins[:2]) / 2 if len(margins) >= 4 else margins[0]
                tr = late - early
            else:
                tr = margins[-1] - margins[0]
            rows.append(("추세", f"{tr:+.0f}%p · "
                         f'{"개선 중" if tr >= 3 else "악화 중" if tr < -3 else "횡보"}',
                         None))
        if rows:
            blocks.append(("3. 어떻게 크고 있나", rows,
                       "여기 영업이익률은 연간입니다. 성장 잠재력 점수의 "
                       "'이익률 추세' 는 분기 기준이라 숫자가 다릅니다."))

        # 4. 주가가 기대하는 것
        rows = []
        per, peg = d.get("per"), d.get("peg")
        if per:
            rows.append(("PER", f"{per:.1f}", None))
            rows.append(("PEG 1 이 되려면", f"연 {per:.0f}% 성장 필요", GRAY))
        if peg:
            rows.append(("PEG", f"{peg:.2f}", None))
        if d.get("tgt") and d.get("price"):
            up = (d["tgt"]/d["price"]-1)*100
            rows.append(("애널 목표가", f"{d['tgt']:,.0f}  {up:+.0f}%", None))
        if rows:
            blocks.append(("4. 지금 주가가 기대하는 것", rows, None))

        for title, rows, note in blocks:
            st.markdown(f'<div style="font-size:.8rem;font-weight:700;'
                        f'color:{CHARCOAL};margin:10px 0 4px">{title}</div>',
                        unsafe_allow_html=True)
            st.markdown("".join(
                f'<div class="metric"><span class="mk">{k}</span>'
                f'<span class="mv"'
                + (f' style="color:{c_}"' if c_ else "") + f'>{v}</span></div>'
                for k, v, c_ in rows), unsafe_allow_html=True)
            if note:
                st.markdown(f'<p class="note">{note}</p>', unsafe_allow_html=True)

        st.markdown('<p class="note" style="margin-top:10px">'
                    '다모다란: "모든 밸류에이션은 틀린다. 문제는 얼마나 틀리느냐다."<br>'
                    '이 지표들은 답이 아니라 질문의 출발점입니다. '
                    '채점(성장 잠재력·장기 보유)에는 들어가지 않습니다.</p>',
                    unsafe_allow_html=True)


def price_chart(t, currency="USD", band=None):
    """1년 일봉 캔들 + 20/50MA + 추세선 + 진입밴드."""
    d = chart_data(t)
    if not d or not d["rows"]:
        return

    rows = d["rows"]
    UP, DN = "#DC2626", "#2563EB"     # 한국식: 상승 빨강, 하락 파랑

    W, H = 680, 230
    PAD_L, PAD_R, PAD_T, PAD_B = 4, 52, 10, 18
    iw = W - PAD_L - PAD_R
    ih = H - PAD_T - PAD_B

    lo = min(r["low"] for r in rows)
    hi = max(r["high"] for r in rows)
    span = hi - lo
    if span <= 0:
        # ★ 2026-09-13 — 1년 내내 고가=저가 (거래정지·데이터 이상) 면
        #   예전에는 그냥 return 이라 차트가 말없이 사라졌다.
        #   왜 없는지 알 수 없으니, 폭을 억지로 만들어 평평한 선이라도 그린다.
        span = abs(hi) * 0.02 or 1.0
    lo -= span * 0.04
    hi += span * 0.04
    span = hi - lo

    n = len(rows)
    step = iw / n
    bw = max(1.0, min(step * 0.62, 6))

    def x(i):
        return PAD_L + step * (i + 0.5)

    def y(v):
        return PAD_T + ih * (1 - (v - lo) / span)

    parts = []

    # 가로 눈금 3개
    for f in (0.0, 0.5, 1.0):
        v = lo + span * f
        yy = y(v)
        parts.append(f'<line x1="{PAD_L}" y1="{yy:.1f}" x2="{PAD_L+iw:.1f}" '
                     f'y2="{yy:.1f}" stroke="#E5E7EB" stroke-width="1"/>')
        fmt = f"{v:,.0f}" if currency == "KRW" else f"{v:,.1f}"
        parts.append(f'<text x="{PAD_L+iw+6:.1f}" y="{yy+3.5:.1f}" font-size="9" '
                     f'fill="#9CA3AF">{fmt}</text>')

    # 캔들
    for i, r in enumerate(rows):
        col = UP if r["close"] >= r["open"] else DN
        xx = x(i)
        parts.append(f'<line x1="{xx:.1f}" y1="{y(r["high"]):.1f}" x2="{xx:.1f}" '
                     f'y2="{y(r["low"]):.1f}" stroke="{col}" stroke-width="0.8"/>')
        top, bot = max(r["open"], r["close"]), min(r["open"], r["close"])
        hgt = max(y(bot) - y(top), 0.8)
        parts.append(f'<rect x="{xx-bw/2:.1f}" y="{y(top):.1f}" width="{bw:.1f}" '
                     f'height="{hgt:.1f}" fill="{col}"/>')

    # 이동평균선
    for key, col, wdt in (("ma20", "#EA580C", 1.3), ("ma50", "#6B7280", 1.1)):
        pts = [f"{x(i):.1f},{y(r[key]):.1f}" for i, r in enumerate(rows)
               if r.get(key)]
        if len(pts) > 2:
            parts.append(f'<polyline points="{" ".join(pts)}" fill="none" '
                         f'stroke="{col}" stroke-width="{wdt}" opacity="0.85"/>')

    # ── 추세선 (최소제곱 회귀)  2026-09-13 추가 ──
    #   캔들과 이동평균만 있으면 "요즘 어떤가" 는 보여도
    #   "1년을 통틀어 어느 쪽으로 가고 있나" 가 안 보인다.
    #   종가에 직선 하나를 맞춰서 밑바탕 방향을 그린다.
    #   이동평균과 달리 최근값에 끌려다니지 않는다.
    trend_pct = None
    try:
        ys = [float(r["close"]) for r in rows]
        m_ = n / 2.0 - 0.5                      # 평균 index
        my = sum(ys) / n
        sxx = sum((i - m_) ** 2 for i in range(n))
        sxy = sum((i - m_) * (ys[i] - my) for i in range(n))
        if sxx > 0:
            b = sxy / sxx                        # 기울기 (봉당)
            a = my - b * m_
            y0, y1 = a, a + b * (n - 1)
            if y0 > 0:
                trend_pct = (y1 / y0 - 1) * 100
            tcol = UP if b >= 0 else DN
            parts.append(
                f'<line x1="{x(0):.1f}" y1="{y(y0):.1f}" '
                f'x2="{x(n-1):.1f}" y2="{y(y1):.1f}" stroke="{tcol}" '
                f'stroke-width="1.6" stroke-dasharray="6 4" opacity="0.55"/>')
    except Exception:
        pass

    # ── 진입밴드 (주황 띠)  2026-09-13 추가 ──
    #   이 선은 차트에서 나온 게 아니다. DCF·PEG·애널 계산에서 나온 값을
    #   사람이 저장해 둔 것이다. 이동평균 교차 같은 신호가 아니라
    #   "내가 침착할 때 정해 둔 가격" 을 그려 주는 것뿐이다.
    if valid_band(band):
        try:
            blo, bhi = float(band[0]), float(band[1])
            ytop, ybot = y(max(blo, bhi)), y(min(blo, bhi))
            ytop_c = max(PAD_T, min(PAD_T + ih, ytop))
            ybot_c = max(PAD_T, min(PAD_T + ih, ybot))
            if ybot_c > ytop_c:
                parts.append(
                    f'<rect x="{PAD_L}" y="{ytop_c:.1f}" width="{iw:.1f}" '
                    f'height="{ybot_c-ytop_c:.1f}" fill="#EA580C" '
                    f'fill-opacity="0.10"/>')
            for yy in (ytop_c, ybot_c):
                parts.append(f'<line x1="{PAD_L}" y1="{yy:.1f}" '
                             f'x2="{PAD_L+iw:.1f}" y2="{yy:.1f}" '
                             f'stroke="#EA580C" stroke-width="1" '
                             f'stroke-dasharray="4 3" opacity="0.75"/>')
            parts.append(f'<text x="{PAD_L+3}" y="{ybot_c+10:.1f}" '
                         f'font-size="8.5" fill="#EA580C" '
                         f'font-weight="700">진입밴드</text>')
        except Exception:
            pass

    # ── 1년 고점 선 + 현재 위치 ──
    try:
        hi_v = max(r["high"] for r in rows)
        yh = y(hi_v)
        parts.append(f'<line x1="{PAD_L}" y1="{yh:.1f}" x2="{PAD_L+iw:.1f}" '
                     f'y2="{yh:.1f}" stroke="#9CA3AF" stroke-width="0.9" '
                     f'stroke-dasharray="2 3" opacity="0.7"/>')
        parts.append(f'<text x="{PAD_L+3}" y="{yh-3:.1f}" font-size="8.5" '
                     f'fill="#9CA3AF">1년 고점</text>')
    except Exception:
        pass

    # 날짜 라벨 (양 끝 + 가운데)
    for i in (0, n // 2, n - 1):
        anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
        parts.append(f'<text x="{x(i):.1f}" y="{H-4}" font-size="9" '
                     f'fill="#9CA3AF" text-anchor="{anchor}">'
                     f'{rows[i]["date"][2:].replace("-", ".")}</text>')

    st.markdown('<div class="sect">주가 흐름 (1년)</div>', unsafe_allow_html=True)
    st.markdown(
        f'<div style="margin:2px 0 6px"><svg viewBox="0 0 {W} {H}" width="100%">'
        + "".join(parts) + "</svg></div>", unsafe_allow_html=True)

    # 기간별 수익률
    cells = []
    for label, key in (("7일", "ret_7d"), ("1개월", "ret_1m"),
                       ("3개월", "ret_3m"), ("6개월", "ret_6m"),
                       ("1년", "ret_1y")):
        v = d.get(key)
        if v is None:
            txt, col = "-", "#9CA3AF"
        else:
            txt = f"{v:+.1f}%"
            col = UP if v > 0 else (DN if v < 0 else "#6B7280")
        cells.append(
            f'<div style="flex:1;text-align:center">'
            f'<div style="font-size:.7rem;color:#9CA3AF">{label}</div>'
            f'<div style="font-size:.85rem;font-weight:700;color:{col}">{txt}</div>'
            f'</div>')
    st.markdown(f'<div style="display:flex;gap:2px;margin-bottom:6px">'
                + "".join(cells) + "</div>", unsafe_allow_html=True)

    # ── 추세 한 줄 요약  2026-09-13 추가 ──
    bits = []
    if trend_pct is not None:
        tc = UP if trend_pct >= 0 else DN
        word = ("꾸준히 우상향" if trend_pct >= 30 else
                "완만한 우상향" if trend_pct >= 5 else
                "방향 없음 (횡보)" if trend_pct > -5 else
                "완만한 우하향" if trend_pct > -30 else "꾸준히 우하향")
        bits.append(f'1년 추세 <b style="color:{tc}">{trend_pct:+.0f}%</b> · {word}')
    try:
        hi_v = max(r["high"] for r in rows)
        cur_ = float(rows[-1]["close"])
        if hi_v > 0:
            off = (cur_ / hi_v - 1) * 100
            oc = DN if off <= -20 else "#6B7280"
            bits.append(f'고점 대비 <b style="color:{oc}">{off:+.0f}%</b>')
    except Exception:
        pass
    if bits:
        st.markdown(f'<div class="trendline">{" &nbsp;·&nbsp; ".join(bits)}</div>',
                    unsafe_allow_html=True)

    st.markdown('<p class="note">빨강 상승 · 파랑 하락 · 주황 20일선 · '
                '회색 50일선 · <b>점선이 1년 추세선</b> (종가에 직선을 맞춘 것으로, '
                '이동평균과 달리 최근값에 끌려다니지 않습니다)</p>',
                unsafe_allow_html=True)


def fp_section(fp):
    """상세 화면용 섹션."""
    st.markdown('<div class="sect">수급 동향</div>', unsafe_allow_html=True)
    if not fp:
        st.markdown('<p class="note">데이터 부족 (상장 60일 미만 또는 조회 실패)</p>',
                    unsafe_allow_html=True)
        return
    lab, col = fp_verdict(fp["score"])
    flags = []
    if fp["diverge"]:
        flags.append("가격 횡보 + OBV 상승 → 조용한 매수 누적")
    if fp["distrib"]:
        flags.append("가격 버팀 + OBV 하락 → 조용한 매도 누적")
    if fp["pullback"]:
        flags.append("50MA 위 눌림목 + 거래량 마름")
    if fp["spike_up"] >= 2:
        flags.append(f"대량거래 상승일 {fp['spike_up']}회/20일")
    if fp["spike_dn"] >= 2:
        flags.append(f"대량거래 하락일 {fp['spike_dn']}회/20일")
    if not flags:
        flags.append("특이 흔적 없음")

    # 고점 대비·이동평균은 바로 위 차트에 이미 나오므로 여기서는 뺀다.
    # 수급 점수를 만든 세 지표만 남긴다.
    rows = [("상승/하락 거래량비 (20일)", f"{fp['updn']:.2f}"),
            ("대량거래일 (양봉/음봉)", f"{fp['spike_up']} / {fp['spike_dn']}"),
            ("OBV 20일 변화", f"{fp['obv_chg']*100:+.0f}% (가격 {fp['px_chg']*100:+.1f}%)")]
    st.markdown(f"""<div style="display:flex;justify-content:space-between;
      align-items:center;margin-bottom:8px">
      <span class="badge" style="background:{col}">{lab}</span>
      <span style="font-size:.9rem;font-weight:700;color:{col}">{fp['score']}/100</span></div>
      <div class="bar-bg"><div class="bar-fl"
      style="width:{fp['score']}%;background:{col}"></div></div>""",
      unsafe_allow_html=True)
    st.markdown("".join(
        f'<div class="metric"><span class="mk">{k}</span>'
        f'<span class="mv">{v}</span></div>' for k, v in rows)
        + '<p class="note" style="margin-top:8px">'
        + "<br>".join("· " + f for f in flags)
        + '<br><br>지난 5년 백테스트에서 이 점수는 이후 수익률과 '
          '상관이 없었습니다(상관계수 -0.03). 매매 신호가 아니라 '
          '"지금 시장이 이 종목을 어떻게 다루고 있나"를 보는 관측값으로 '
          '쓰세요.</p>',
        unsafe_allow_html=True)





def item_label(k, mode):
    """채점 항목 이름을 화면용으로 다듬는다.

    ★ 2026-09-13 — '밸류에이션' 이 성장·장기 양쪽에 똑같은 이름으로 뜨는데
      보는 것이 서로 다르다.
        성장 잠재력 > 밸류에이션 : PEG (성장률 대비 PER 이 싼가)
        장기 보유   > 밸류에이션 : PER 절대수준 (그냥 비싼가)
      같은 이름이라 "왜 점수가 다르지?" 하게 된다. 표시만 구분한다.
      채점 키(TEN_MAX/LT_MAX)는 건드리지 않으므로 점수는 그대로다.
    """
    if k == "밸류에이션":
        return "밸류에이션 (성장 대비)" if mode == "ten" else "밸류에이션 (PER 절대)"
    return k


def macro_bar():
    """맨 위 시장 상황 — VIX · 원달러.

    ★ 2026-09-13 — 종목만 보다 보면 시장 전체가 어떤지 안 보인다.
      표시 전용이다. 채점에는 연결하지 않는다.
      특히 환율은 USD_KRW(시총 채점에 걸림)를 대체하지 않는다.
    """
    try:
        m = macro()
    except Exception:
        return
    if not m or (m.get("vix") is None and m.get("usdkrw") is None):
        return

    cells = []
    v = m.get("vix")
    if v is not None:
        lab, col = vix_mood(v)
        ch = m.get("vix_chg")
        cells.append(("VIX", f"{v:.1f}", lab, col, ch))
    r = m.get("usdkrw")
    if r is not None:
        ch = m.get("usdkrw_chg")
        cells.append(("원/달러", f"{r:,.0f}", "", CHARCOAL, ch))

    html = ""
    for k, val, lab, col, ch in cells:
        chtxt = ""
        if ch is not None:
            c2 = "#DC2626" if ch >= 0 else "#2563EB"
            chtxt = (f'<span style="color:{c2};font-size:.66rem;'
                     f'font-weight:600;margin-left:4px">{ch:+.1f}%</span>')
        html += (f'<div class="mcell"><span class="mk2">{k}</span>'
                 f'<span class="mv2" style="color:{col}">{val}</span>{chtxt}'
                 + (f'<span class="ml2" style="color:{col}">{lab}</span>'
                    if lab else "") + '</div>')

    # 실험 기간에는 시총 채점 환율을 고정해 두었으므로, 벌어지면 알려 준다
    note = ""
    fixed = m.get("usdkrw_fixed")
    if r and fixed and abs(r / fixed - 1) > 0.05:
        note = (f'<div class="mnote">시총 환산은 {fixed:,}원 고정입니다 '
                f'(현재 {r:,.0f}원). 실험 중 환율이 점수를 흔들지 않게 '
                f'일부러 고정해 둔 값입니다.</div>')

    st.markdown(f'<div class="macro">{html}</div>{note}', unsafe_allow_html=True)


def qhead(no, text, sub="", ref=False):
    """번호 붙은 질문형 섹션 제목.

    ★ 2026-09-13 — 예전에는 '성장 잠재력' '시장 지표' 처럼
      항목 이름만 있었다. 여덟 덩어리가 전부 똑같아 보여서
      어디부터 읽어야 할지 알 수 없었다.
      질문으로 바꾸고 번호를 붙여 읽는 순서를 눈으로 보여 준다.
      참고용 섹션(ref)은 번호를 회색으로 해서 비중을 낮춘다.
    """
    st.markdown(
        f'<div class="qhd"><span class="qno{" ref" if ref else ""}">{no}</span>'
        f'<span class="qtx">{text}</span>'
        + (f'<span class="qsub">{sub}</span>' if sub else "")
        + '</div>', unsafe_allow_html=True)


def price_verdict(d):
    """③ 가격 판정.

    ★ 2026-09-13 — 설계 의도를 적어 둔다.

      맨 위에 오는 것은 '적정주가' 가 아니라
        "지금 이 가격은 연 몇 % 성장을 요구하고 있는가"
      이다. 이건 예측이 아니라 지금 가격에 박힌 기대치를 되읽은 것이라
      미래를 안 맞혀도 참이다. 사람이 할 판단은 하나로 좁혀진다 —
      "그 성장이 가능한가?"

      적정가 세 개(DCF·PEG·애널)는 그 아래 '범위' 로만 둔다.
      셋 다 정확하지 않기 때문이다. 오히려 셋이 크게 벌어져 있다는 사실
      자체가 "이 회사는 값을 매기기 어렵다" 는 정보다.
    """
    px = d.get("price")
    if not px:
        return
    cur = d.get("currency", "USD")
    unit = "원" if cur == "KRW" else "$"

    def fmt(v):
        return f"{unit}{v:,.0f}" if cur == "KRW" else f"{unit}{v:,.2f}"

    # ── 지금 가격이 요구하는 성장률 ──
    req = implied_growth(d)
    if req is not None:
        act = d.get("growth")
        per = d.get("per")
        if act is not None:
            gap = act - req
            col = ORANGE if gap >= 0 else "#DC2626"
            verdict = ("실제 성장이 요구치를 넘고 있습니다"
                       if gap >= 0 else "실제 성장이 요구치에 못 미칩니다")
            cmp_html = (f'<div class="reqcmp">실제 최근 성장률 '
                        f'<b style="color:{col}">{act:.0f}%</b>'
                        f'<span style="color:{col};font-weight:700">'
                        f'  ({gap:+.0f}%p)</span><br>'
                        f'<span style="color:{col};font-size:.76rem">'
                        f'{verdict}</span></div>')
        else:
            cmp_html = ('<div class="reqcmp">실제 성장률을 못 구해 '
                        '비교하지 못했습니다</div>')
        st.markdown(
            f'<div class="reqbox">'
            f'<div class="reqk">지금 가격이 요구하는 것</div>'
            f'<div class="reqv">연 {req:.0f}% 성장 <span '
            f'style="font-size:.8rem;font-weight:600;color:#9CA3AF">'
            f'× 10년</span></div>'
            f'<div class="reqsub">PER {per:.1f} 를 정당화하려면 필요한 값입니다. '
            f'예측이 아니라 지금 가격에 이미 들어 있는 기대치입니다.</div>'
            f'{cmp_html}</div>', unsafe_allow_html=True)

    # ── 적정가 범위 ──
    wacc = st.session_state.get(f"fvwacc_{d['ticker']}", 9.0)
    fr = fair_range(d, wacc=wacc)
    items = fr.get("items") or []

    if items:
        chips = " · ".join(f"{k} {fmt(v)}" for k, v, _ in items)
        vals = sorted(v for _, v, _ in items)
        n_est = len(vals)
        mid = (vals[n_est//2] if n_est % 2
               else (vals[n_est//2 - 1] + vals[n_est//2]) / 2)
        ratio = px / mid * 100

        # ★ 2026-09-13 — 값이 셋일 때만 '중앙값' 이 제 역할을 한다.
        #   둘이면 그냥 평균이고, 튀는 하나를 걸러 주지 못한다.
        #   실제로 이런 일이 있었다 (PER 10.6 · 매출 -6% 인 종목):
        #       DCF 110,985 · 애널 169,333  → 중앙값 140,159 → "쌉니다"
        #       애널을 빼면 110,985          → 현재가의 95%  → "적정 범위"
        #   애널 3명이 만든 숫자 하나가 판정을 뒤집은 것이다.
        midlab = "중앙값" if n_est >= 3 else ("평균" if n_est == 2 else "단일값")
        why = {"DCF": fr.get("dcf_fail"),
               "PEG": ("성장률이 없거나 음수" if not d.get("growth")
                       or (d.get("growth") or 0) <= 0 else
                       "PER 을 못 구함" if not d.get("per") else None),
               "애널": "목표가 없음"}
        missing = [f"{k} 없음 ({why.get(k)})" for k in ("DCF", "PEG", "애널")
                   if k not in [i[0] for i in items] and why.get(k)]

        pos = fr.get("pos")
        bar = ""
        if pos is not None:
            p = max(0, min(100, pos))
            bar = (f'<div class="fbar"><div class="fdot" '
                   f'style="left:{p:.0f}%"></div></div>'
                   f'<div class="fends"><span>{fmt(fr["lo"])}</span>'
                   f'<span>{fmt(fr["hi"])}</span></div>')

        rcol = "#DC2626" if ratio > 115 else ORANGE if ratio < 85 else CHARCOAL
        rtxt = ("적정가보다 비쌉니다" if ratio > 115
                else "적정가보다 쌉니다" if ratio < 85 else "적정 범위 안입니다")

        warns = []
        sp = fr.get("spread")
        if sp and sp >= 3:
            warns.append(f'{n_est}개 방법의 값이 {sp:.1f}배나 벌어져 있습니다. '
                         '이런 회사는 어떤 적정주가도 믿을 게 못 됩니다. '
                         '위의 요구 성장률만 보세요.')

        # 애널 목표가가 절반 이상을 차지하는가
        an = next((v for k, v, _ in items if k == "애널"), None)
        if an is not None and n_est <= 2:
            others = [v for k, v, _ in items if k != "애널"]
            nan_ = d.get("n_analyst")
            bit = (f'<b>애널리스트 목표가가 적정가의 절반을 차지합니다'
                   + (f' (애널 {nan_}명)' if nan_ else "") + '.</b> '
                   '세 방법 중 가장 높게 잡히는 값입니다. ')
            if others:
                o = others[0]
                r2 = px / o * 100
                t2 = ("비쌈" if r2 > 115 else "쌈" if r2 < 85 else "적정")
                bit += (f'애널을 빼면 적정가는 {fmt(o)} 이고 '
                        f'현재가는 그것의 <b>{r2:.0f}%</b> ({t2}) 입니다.')
            warns.append(bit)
        elif n_est <= 2:
            warns.append(f'적정가가 {n_est}개뿐이라 튀는 값을 걸러 주지 못합니다.')

        if missing:
            warns.append("계산되지 않은 것 — " + " · ".join(missing))

        warn = "".join(f'<div class="fwarn">{w}</div>' for w in warns)

        st.markdown(
            f'<div class="fbox"><div class="reqk">적정가 범위'
            + (f' <span style="font-weight:400;color:#9CA3AF">({n_est}개)</span>'
               if n_est < 3 else "") + '</div>'
            f'<div class="fchips">{chips}</div>{bar}'
            f'<div class="fratio">현재 {fmt(px)} — 적정가({midlab} {fmt(mid)})의 '
            f'<b style="color:{rcol}">{ratio:.0f}%</b><br>'
            f'<span style="color:{rcol};font-size:.76rem">{rtxt}</span></div>'
            f'{warn}</div>', unsafe_allow_html=True)

        with st.expander("적정가 계산 손보기"):
            st.slider("자본비용 (%)", 5.0, 15.0, 9.0, 0.5,
                      key=f"fvwacc_{d['ticker']}",
                      help="높일수록 적정가가 내려간다. 위험한 회사일수록 높게")
            mos = st.slider("안전마진 (%)", 0, 50, 30, 5,
                            key=f"mos_{d['ticker']}",
                            help="적정가에서 이만큼 깎은 값을 매수가로 본다")
            buy = mid * (1 - mos / 100)
            st.markdown(
                f'<div class="buybox">안전마진 {mos}% 적용 매수가 '
                f'<b>{fmt(buy)}</b><br>'
                f'<span style="font-size:.74rem;color:#6B7280">'
                f'{"현재가가 이미 이 아래입니다" if px <= buy else f"현재가 {fmt(px)} — 아직 {(px/buy-1)*100:.0f}% 높습니다"}'
                f'</span></div>', unsafe_allow_html=True)

            # ── 계산값을 진입밴드로 저장 ──
            #   ★ 2026-09-13 — "매수 신호" 를 만들지 않은 이유가 여기 있다.
            #     차트에서 뽑은 신호는 이 도구의 백테스트에서 상관 -0.03 이었다.
            #     대신 침착할 때 계산해 둔 가격을 저장해 두고,
            #     시장이 거기 오면 알려 주는 쪽으로 만든다.
            #     앱이 판단해 주는 게 아니라, 내가 한 판단을 지켜 주는 장치다.
            st.markdown(f'<div class="note">아래 버튼을 누르면 '
                        f'<b>{fmt(buy)} ~ {fmt(mid)}</b> 구간이 진입밴드로 저장됩니다. '
                        f'차트에 주황 띠로 그려지고, 대시보드에서 이 구간에 '
                        f'들어오면 알려 줍니다.</div>', unsafe_allow_html=True)
            if st.button("이 가격대를 진입밴드로 저장", key=f"setband_{d['ticker']}",
                         use_container_width=True):
                stt = load_state()
                tk = d["ticker"]
                stt["bands"] = dict(stt.get("bands") or {})
                # ★ band_text 가 소수점 2자리로 그리므로 저장도 2자리로 맞춘다.
                #   4자리로 저장하면 관심종목 탭이 화면값(2자리)과 다르다고 보고
                #   들어갈 때마다 조용히 덮어써서 저장이 한 번 더 일어난다.
                stt["bands"][tk] = [round(buy, 2), round(mid, 2)]
                added = tk not in stt["tickers"]
                if added:
                    stt["tickers"] = list(stt["tickers"]) + [tk]
                save_state(stt)
                st.success(f"{tk} 진입밴드 {fmt(buy)} ~ {fmt(mid)} 저장했습니다."
                           + ("  관심종목에도 추가했습니다." if added else ""))
                st.rerun()

            # 가정을 전부 드러낸다 — 숫자가 정밀해 보이지 않게
            for k, v, extra in items:
                if k == "DCF" and extra:
                    st.markdown(
                        f'<div class="note">DCF 가정 — 성장 {extra["growth"]:.0f}% · '
                        f'이익률 {extra["margin"]:.0f}% · 재투자 {extra["reinvest"]:.0f}% · '
                        f'자본비용 {extra["wacc"]:.1f}% · 영구성장 {extra["terminal"]:.1f}%<br>'
                        f'이 값의 <b>{extra["tv_share"]:.0f}%</b> 가 10년 뒤 이후 '
                        f'가정에서 나옵니다'
                        + (f'<br>{extra["note"]}' if extra.get("note") else "")
                        + '</div>', unsafe_allow_html=True)
            st.markdown(f'<div class="note">{"세" if n_est >= 3 else f"이 {n_est}"} '
                        '방법 모두 정확하지 않습니다. '
                        '애널 목표가는 구조적으로 높게 잡히고, DCF 는 먼 미래 '
                        '가정이 값을 결정하고, PEG 는 어림셈입니다. '
                        '하나를 믿지 말고 범위로 보세요.'
                        + ('<br><b>지금은 값이 2개뿐이라 서로를 견제하지 '
                           '못합니다. 위의 요구 성장률을 더 믿으세요.</b>'
                           if n_est <= 2 else "")
                        + '</div>', unsafe_allow_html=True)
    elif fr.get("dcf_fail"):
        st.markdown(f'<div class="refbox">적정가를 계산하지 못했습니다 '
                    f'({fr["dcf_fail"]}).</div>', unsafe_allow_html=True)


def axis_pct(items, mx, axes, want):
    """레이더의 특정 축 하나만 퍼센트로 뽑는다 (레이더와 같은 계산)."""
    got = {k: s for k, s, _ in items}
    for label, keys in axes:
        if label != want:
            continue
        g = sum(got[k] for k in keys if k in got)
        a = sum(mx[k] for k in keys if k in got)
        return (g / a * 100) if a else None
    return None


def summary_card(d):
    """맨 위 한 줄 요약.

    ★ 2026-09-13 — 새로 채점하지 않는다. 이미 계산된 값을
      한자리에 모아 보여 줄 뿐이다. 판단 기준(임계값)도
      기존 ten_verdict / lt_verdict / fp_verdict 를 그대로 쓴다.

      이게 필요한 이유:
        CRDO 는 다모다란 100점인데 성장은 상위 43%, 수급은 11 이다.
        화면 하나만 보고 "완벽한 회사" 로 읽는 사고를 막으려면
        네 가지가 같은 자리에 있어야 한다.
    """
    ti = score_ten(d)
    li = score_lt(d)
    if not ti and not li:
        return

    cells = []

    # ① 성장 잠재력
    if ti:
        _, _, tp = pctile(ti, TEN_MAX)
        tag, col = ten_verdict(tp)
        cells.append(("성장 잠재력", f"{tp:.0f}", "%", tag, col))
    # ② 장기 보유 적합도
    if li:
        _, _, lp = pctile(li, LT_MAX)
        tag, col = lt_verdict(lp)
        cells.append(("장기 보유", f"{lp:.0f}", "%", tag, col))

    # ③ 가격 — 레이더의 '주가매력' 축을 그대로 쓴다
    ap = axis_pct(ti, TEN_MAX, AXES_TEN, "주가매력") if ti else None
    if ap is not None:
        # 표시용 눈대중이다. 채점에 쓰이는 기준이 아니다.
        lab, col = (("싼 편", ORANGE) if ap >= 60 else
                    ("보통", AMBER) if ap >= 40 else ("비싼 편", "#9CA3AF"))
        cells.append(("가격 매력", f"{ap:.0f}", "%", lab, col))

    # ④ 수급 — 예측력이 없다고 검증된 값이라 맨 뒤에 둔다
    fp = footprint(d["ticker"])
    if fp:
        lab, col = fp_verdict(fp["score"])
        cells.append(("수급 (참고)", f"{fp['score']}", "", lab, col))

    if not cells:
        return

    grid = "".join(
        f'<div class="sumc"><div class="sumk">{k}</div>'
        f'<div class="sumv" style="color:{col}">{v}'
        f'<span style="font-size:.8rem;font-weight:600">{unit}</span></div>'
        f'<div class="suml" style="color:{col}">{lab}</div></div>'
        for k, v, unit, lab, col in cells)

    say = _summary_say(d, ti, li, ap, fp)
    st.markdown(f'<div class="sum"><div class="sumg">{grid}</div>'
                f'<div class="sumsay">{say}</div></div>',
                unsafe_allow_html=True)


def _summary_say(d, ti, li, ap, fp):
    """네 숫자를 사람 말로 한 번 풀어 준다.

    점수를 새로 만드는 게 아니라, 위 네 칸을 읽는 법을 적어 주는 것이다.
    기준선은 전부 기존 verdict 함수의 경계를 그대로 쓴다.
    """
    parts = []

    tp = pctile(ti, TEN_MAX)[2] if ti else None
    lp = pctile(li, LT_MAX)[2] if li else None

    # 회사 자체
    best = max([x for x in (tp, lp) if x is not None], default=None)
    if best is None:
        parts.append("채점할 데이터가 모자랍니다.")
    elif best >= 70:
        parts.append("<b>회사 자체는 좋은 편입니다.</b>")
    elif best >= 52:
        parts.append("<b>회사는 무난한 편입니다.</b>")
    else:
        parts.append("<b>회사 점수가 낮습니다.</b> 싸 보여도 이유가 있을 수 있습니다.")

    # 성장과 장기가 엇갈리는 경우 — 이게 꽤 자주 나오는데 설명이 없었다
    if tp is not None and lp is not None and abs(tp - lp) >= 18:
        hi, lo = ("성장", "장기") if tp > lp else ("장기", "성장")
        parts.append(f"{hi} 쪽은 높고 {lo} 쪽은 낮습니다 — "
                     f"{'지금 잘 나가지만 오래 갈지는 덜 확실' if hi == '성장' else '탄탄하지만 성장 속도는 느림'}.")

    # 가격
    if ap is not None:
        if ap < 40 and best is not None and best >= 70:
            parts.append("다만 <b>가격이 비쌉니다.</b> "
                         "좋은 회사와 좋은 매수는 다른 문제입니다.")
        elif ap < 40:
            parts.append("가격도 싸지 않습니다.")
        elif ap >= 60:
            parts.append("가격은 괜찮은 편입니다.")

    # 수급은 참고만
    if fp and fp["score"] <= 30:
        parts.append("수급은 나쁘지만, 검증 결과 수급은 예측력이 없었습니다(참고만).")

    return " ".join(parts)


def radar_svg(items, mx, mode, center_score, center_col, axes=None):
    """items=[(k,s,v)], mx=만점dict → 5축 퍼센트 레이더 SVG 문자열.
    axes 를 직접 주면 그것을 쓰고, 없으면 mode 로 고른다."""
    got = {k: s for k, s, _ in items}
    if axes is None:
        axes = AXES_TEN if mode == "ten" else AXES_LT

    vals = []
    for label, keys in axes:
        g = sum(got[k] for k in keys if k in got)
        a = sum(mx[k] for k in keys if k in got)
        vals.append((label, (g / a * 100) if a else None))

    import math
    W, H = 300, 272
    CX, CY, R = 150.0, 134.0, 78.0

    def pt(i, r):
        ang = math.radians(-90 + i * 72)
        return CX + r * math.cos(ang), CY + r * math.sin(ang)

    # 배경 5각형(3겹) + 축선
    grid = ""
    for f in (1.0, 0.66, 0.33):
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in (pt(i, R * f) for i in range(5)))
        grid += (f'<polygon points="{pts}" fill="{"#EEF2FF" if f == 1.0 else "none"}" '
                 f'stroke="#D7DBE8" stroke-width="1"/>')
    for i in range(5):
        x, y = pt(i, R)
        grid += (f'<line x1="{CX}" y1="{CY}" x2="{x:.1f}" y2="{y:.1f}" '
                 f'stroke="#E5E7EB" stroke-width="1"/>')

    # 데이터 폴리곤
    dpts, dots = [], ""
    for i, (label, v) in enumerate(vals):
        r = R * (max(v, 0) / 100) if v is not None else 0
        x, y = pt(i, max(r, 2))
        dpts.append(f"{x:.1f},{y:.1f}")
        if v is not None:
            dots += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{center_col}"/>'
    poly = (f'<polygon points="{" ".join(dpts)}" fill="{center_col}" fill-opacity="0.18" '
            f'stroke="{center_col}" stroke-width="2" stroke-linejoin="round"/>')

    # 축 라벨 + 점수
    labs = ""
    for i, (label, v) in enumerate(vals):
        lx, ly = pt(i, R + 24)
        if i == 0:
            ly -= 6
        num = f"{v:.0f}" if v is not None else "-"
        labs += (f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" '
                 f'font-size="11" fill="#6B7280">{label}</text>'
                 f'<text x="{lx:.1f}" y="{ly + 15:.1f}" text-anchor="middle" '
                 f'font-size="13" font-weight="700" fill="#2F3437">{num}</text>')

    center = (f'<text x="{CX}" y="{CY + 9:.1f}" text-anchor="middle" font-size="26" '
              f'font-weight="800" fill="{center_col}">{center_score:.0f}</text>')

    return (f'<div style="display:flex;justify-content:center;margin:4px 0 10px">'
            f'<svg viewBox="0 0 {W} {H}" width="100%" style="max-width:320px">'
            f'{grid}{poly}{dots}{center}{labs}</svg></div>')


def card(d, mode, band=None, rank=None):
    """대시보드 카드.

    ★ 2026-09-13 — 예전에는 라디오로 고른 점수 하나만 보였다.
      관심종목을 훑어보는 화면인데 성장을 보다가 장기를 보려면
      토글을 눌러야 했고, 그러면 비교가 머릿속에서 끊긴다.
      성장·장기·가격 세 줄을 한 카드에 넣어 토글 없이 비교되게 한다.
      라디오는 이제 '무엇으로 정렬할까' 만 정한다.
    """
    ti, li = score_ten(d), score_lt(d)

    rows = []
    for nm, items, mx, vf in (("성장", ti, TEN_MAX, ten_verdict),
                              ("장기", li, LT_MAX, lt_verdict)):
        if not items:
            continue
        _, _, p = pctile(items, mx)
        tag, col = vf(p)
        rows.append((nm, p, tag, col))

    # ★ 2026-09-13 — 다모다란을 카드에도 넣는다.
    #   종목을 나란히 놓고 비교하는 화면인데 이것만 빠져 있었다.
    #   cli 의 watch 화면과 맞춘 것이기도 하다.
    #
    #   순서는 상세 화면의 ①②③ 과 같게 둔다 (성장·장기 → 다모 → 가격).
    #   두 화면을 오갈 때 읽는 순서가 달라지면 안 된다.
    #
    #   ※ 맨 위 요약 카드에는 일부러 넣지 않았다.
    #     CRDO 처럼 다모다란만 100 인 종목에서, 맥락 없이 큰 숫자로
    #     제일 먼저 눈에 띄면 오늘 개편한 이유가 무색해진다.
    #     상세 ②번에는 성장·장기 칩과 경고가 같이 붙어 맥락이 있다.
    di = score_damo(d)
    if di:
        _, _, dp = pctile(di, DAMO_MAX)
        tag, col = damo_verdict(dp)
        rows.append(("다모", dp, tag, col))

    # 가격 매력 — 성장 레이더의 '주가매력' 축 (성장 점수 안의 일부다)
    ap = axis_pct(ti, TEN_MAX, AXES_TEN, "주가매력") if ti else None
    if ap is not None:
        tag, col = (("싼 편", ORANGE) if ap >= 60 else
                    ("보통", AMBER) if ap >= 40 else ("비싼 편", "#9CA3AF"))
        rows.append(("가격", ap, tag, col))

    srows = "".join(
        f'<div class="srow"><span class="sname">{nm}</span>'
        f'<span class="snum" style="color:{col}">{p:.0f}</span>'
        f'<span class="sbar"><span class="sfil" '
        f'style="width:{max(0, min(100, p)):.0f}%;background:{col}"></span></span>'
        f'<span class="stag" style="color:{col}">{tag}</span></div>'
        for nm, p, tag, col in rows)

    ch = d["chg"]
    cls = "up" if (ch or 0) >= 0 else "dn"
    chtxt = f"{ch:+.2f}%" if ch is not None else ""

    # 아래 한 줄에 진입밴드와 수급을 회색으로 — 참고값이라는 걸 크기로 말한다
    foot = []
    bs = band_status(d["price"], band)
    if bs:
        label, bcol, _ = bs
        foot.append(f'<span>진입밴드 {band_text(band)} · '
                    f'<b style="color:{bcol}">{label}</b></span>')
    fp = footprint(d["ticker"])
    if fp:
        lab, _c = fp_verdict(fp["score"])
        foot.append(f'<span>수급 {fp["score"]} · {lab}</span>')
    footh = f'<div class="cfoot">{"".join(foot)}</div>' if foot else ""

    rk = f'<span class="rankno">{rank}</span>' if rank else ""

    st.markdown(f"""<div class="card">
      <div class="card-hd">
        <div>{rk}<span class="tkr">{d['ticker']}</span>
             <div class="nm">{(d['name'] or '')[:30]}</div></div>
        <div style="text-align:right">
          <div class="px">{_px(d)}</div><div class="{cls}">{chtxt}</div></div>
      </div>
      {srows}{footh}
    </div>""", unsafe_allow_html=True)


def detail(d, band=None):
    st.markdown(f"### {d['ticker']}")
    st.caption(f"{d['name']}  ·  {d['sector'] or ''}")

    c1, c2, c3 = st.columns(3)
    c1.metric("현재가", _px(d),
              f"{d['chg']:+.2f}%" if d["chg"] is not None else None)
    c2.metric("시총", won(d["mcap_krw"]))
    e, dd_ = dday(d["earnings"])
    c3.metric("실적발표", f"D{-dd_:+d}" if dd_ is not None and -30 < dd_ < 300 else "-")

    # ★ 2026-09-13 — 맨 위에 요약. 스크롤하기 전에 결론이 보이게 한다.
    summary_card(d)

    # ═══ ① 좋은 회사인가 ═══
    qhead(1, "좋은 회사인가", "성장 · 장기 보유")
    for mode, title, mx, vf in (("ten", "성장 잠재력", TEN_MAX, ten_verdict),
                                ("lt", "장기 보유 적합도", LT_MAX, lt_verdict)):
        items = score_ten(d) if mode == "ten" else score_lt(d)
        if not items:
            continue
        got, avail, p = pctile(items, mx)
        tag, col = vf(p)
        st.markdown(f'<div class="sect">{title}</div>', unsafe_allow_html=True)
        st.markdown(f"""<div style="display:flex;justify-content:space-between;
          align-items:center;margin-bottom:8px">
          <span class="badge" style="background:{col}">{tag}</span>
          <span class="bigpct" style="color:{col}">{p:.0f}<span
            style="font-size:.85rem">%</span><span class="bigsub">{got}/{avail}</span>
          </span></div>
          <div class="bar-bg"><div class="bar-fl"
          style="width:{p:.0f}%;background:{col}"></div></div>""",
          unsafe_allow_html=True)
        st.markdown(radar_svg(items, mx, mode, p, col), unsafe_allow_html=True)
        rows = "".join(
            f'<div class="metric"><span class="mk">{item_label(k, mode)}</span>'
            f'<span class="mv">{v} <span style="color:#9CA3AF;font-weight:400">'
            f'{s}/{mx[k]}</span></span></div>'
            for k, s, v in items)
        st.markdown(rows, unsafe_allow_html=True)

    # ═══ ② 자본을 잘 굴리나 ═══
    #   예전에는 맨 아래에 있었다. 다모다란 점수가 높게 나오는 종목일수록
    #   위의 성장·장기 점수와 엇갈리는데, 끝까지 스크롤해야 보여서
    #   그 엇갈림을 알아채기 어려웠다. 위로 올린다.
    qhead(2, "자본을 잘 굴리나", "다모다란 관점")
    damo_section(d)

    # ═══ ③ 지금 가격이 괜찮나 ═══
    qhead(3, "지금 가격이 괜찮나", "요구 성장률 · 적정가 범위")
    price_verdict(d)
    price_chart(d['ticker'], d.get('currency', 'USD'), band=band)

    rows = []
    bs = band_status(d["price"], band)
    if bs:
        label, bcol, _ = bs
        rows.append(("진입밴드", f"{band_text(band)} · {label}"))
    if d["tgt"] and d["price"]:
        up = (d["tgt"]/d["price"]-1)*100
        n = f" (애널 {d['n_analyst']}명)" if d["n_analyst"] else ""
        rows.append(("목표가", f"{d['tgt']:,.0f}  {up:+.0f}%{n}"))
    if d["rec"]:
        rows.append(("컨센서스", d["rec"].upper()))
    if d["short_pct"] is not None:
        rows.append(("공매도", f"{d['short_pct']:.1f}%"))
    if d["inst"] is not None:
        rows.append(("기관보유", f"{d['inst']:.1f}%"))
    if d["px1y"] is not None:
        rows.append(("1년 수익률", f"{d['px1y']:+.1f}%"))
    st.markdown("".join(
        f'<div class="metric"><span class="mk">{k}</span>'
        f'<span class="mv">{v}</span></div>' for k, v in rows),
        unsafe_allow_html=True)

    # ═══ ④ 요즘 분위기는 (참고) ═══
    #   ★ 2026-09-13 — 백테스트 상관계수 -0.03. 예측력이 없다고
    #     이미 검증된 값인데, 화면에서는 다른 점수와 똑같은 무게로
    #     보였다. 번호를 회색으로 두고 맨 뒤로 내려 비중을 낮춘다.
    qhead(4, "요즘 분위기는", "참고용", ref=True)
    # fp_section 안에 이미 "상관 -0.03" 설명이 있으므로 여기서 또 쓰지 않는다
    st.markdown('<div class="dim">', unsafe_allow_html=True)
    fp_section(footprint(d['ticker']))
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<p class="note" style="margin-top:20px">'
                '체크리스트지 추천이 아닙니다. '
                '자동 수집값은 누락·오류가 있을 수 있으니 최종 판단 전 '
                '실적발표 원문을 확인하세요.</p>', unsafe_allow_html=True)


def main():
    stt = load_state()
    watch, bands = stt["tickers"], stt["bands"]
    macro_bar()          # ★ 2026-09-13 — 종목 전에 시장부터
    tab1, tab2, tab3 = st.tabs(["대시보드", "종목 조회", "관심종목"])

    with tab1:
        if not watch:
            st.info("관심종목 탭에서 종목을 추가하면 여기에 표시됩니다.")
        else:
            c_a, c_b = st.columns([3, 1])
            with c_b:
                # 저장된 데이터를 비우고 야후에서 새로 받아온다.
                # (평소에는 15분간 재사용하므로 눌러도 값이 거의 안 바뀐다.
                #  야후 자체가 15~20분 지연이라 완전 실시간은 되지 않는다.)
                if st.button("새로 받기", use_container_width=True,
                             help="저장된 데이터를 비우고 다시 조회합니다"):
                    st.cache_data.clear()
                    st.rerun()
            with c_a:
                mode = st.radio("정렬", ["성장", "장기"], horizontal=True,
                                label_visibility="collapsed",
                                help="어느 점수가 높은 순으로 줄 세울지")
            m = "ten" if mode == "성장" else "lt"

            # 데이터 수집 + 알림 판정
            alerts = []        # 실적 D-day
            entries = []       # 진입밴드 도달
            data = []
            prog = st.progress(0.0)
            for i, t in enumerate(watch):
                d = fetch(t)
                prog.progress((i+1)/len(watch))
                if d:
                    data.append(d)
                    e, dd_ = dday(d["earnings"])
                    if dd_ is not None and 0 <= dd_ <= 14:
                        alerts.append((d["ticker"], e, dd_))
                    bs = band_status(d["price"], bands.get(d["ticker"]))
                    if bs and bs[2]:
                        entries.append((d["ticker"], d["price"],
                                        bands.get(d["ticker"])))
            prog.empty()

            # 진입밴드 배너 (주황) — 실적 배너보다 위
            for tk_, px_, bd_ in entries:
                st.markdown(f'<div class="entry">◆ <b>{tk_}</b> 진입밴드 도달 — '
                            f'{px_:,.0f} (밴드 {band_text(bd_)}) · 분할매수 계획 확인</div>'
                            if (bd_ and bd_[0] >= 1000) else
                            f'<div class="entry">◆ <b>{tk_}</b> 진입밴드 도달 — '
                            f'{px_:,.2f} (밴드 {band_text(bd_)}) · 분할매수 계획 확인</div>',
                            unsafe_allow_html=True)

            # 실적 D-day 배너 (차콜)
            for tk_, e, dd_ in sorted(alerts, key=lambda x: x[2]):
                st.markdown(f'<div class="dday">◆ <b>{tk_}</b> 실적발표 '
                            f'{e:%m/%d} · <b>D-{dd_}</b> — 발표 후 다시 확인</div>',
                            unsafe_allow_html=True)

            # 수급이 한쪽으로 크게 치우친 종목.
            # ★ 2026-09-13 — 예전에는 종목마다 실적발표와 똑같은 크기의
            #   배너를 하나씩 띄웠다. 관심종목이 늘면 화면이 배너로 덮이고,
            #   무엇보다 수급은 검증 결과 예측력이 없었다(-0.03).
            #   예측력 없는 값이 제일 큰 목소리를 내고 있었던 셈이다.
            #   한 줄로 묶고 회색으로 낮춘다. 정보는 그대로 두되 크기만 줄인다.
            hot, cold = [], []
            for d in data:
                fp_ = footprint(d["ticker"])
                if not fp_:
                    continue
                if fp_["score"] >= 70:
                    hot.append(f'{d["ticker"]} {fp_["score"]}')
                elif fp_["score"] <= 40:
                    cold.append(f'{d["ticker"]} {fp_["score"]}')
            if hot or cold:
                bits = []
                if hot:
                    bits.append("매수 우위 " + ", ".join(hot))
                if cold:
                    bits.append("매도 우위 " + ", ".join(cold))
                st.markdown(
                    '<div class="refbox" style="margin-bottom:8px">수급 치우침 — '
                    + " · ".join(bits)
                    + '<br><span style="font-size:.7rem">검증상 예측력이 없는 '
                      '값입니다. 매매 근거가 아니라 "왜 이런지" 확인용입니다.'
                      '</span></div>', unsafe_allow_html=True)

            # ★ 2026-09-13 — 예전에는 관심종목에 넣은 순서 그대로였다.
            #   훑어봐도 뭐가 위인지 안 보였다. 고른 점수로 줄 세운다.
            def _key(d):
                it = score_ten(d) if m == "ten" else score_lt(d)
                mx = TEN_MAX if m == "ten" else LT_MAX
                return -(pctile(it, mx)[2] if it else -1)
            for i_, d in enumerate(sorted(data, key=_key), 1):
                card(d, m, bands.get(d["ticker"]), rank=i_)
                if st.button(f"{d['ticker']} 상세", key=f"b{d['ticker']}",
                             use_container_width=True):
                    st.session_state["sel"] = d["ticker"]
                    st.rerun()

            if st.session_state.get("sel"):
                st.divider()
                dd2 = fetch(st.session_state["sel"])
                if dd2:
                    detail(dd2, bands.get(st.session_state["sel"]))
                if st.button("닫기", use_container_width=True):
                    st.session_state["sel"] = None
                    st.rerun()

    with tab2:
        t = st.text_input("티커", placeholder="예: MU, ALAB, ANET").strip().upper()
        if t:
            with st.spinner("불러오는 중"):
                d = fetch(t)
            if d is None:
                st.error("데이터를 찾을 수 없습니다. 티커를 확인하세요.")
            else:
                detail(d, bands.get(t))
                if t not in watch:
                    if st.button("관심종목에 추가", use_container_width=True):
                        stt["tickers"] = watch + [t]
                        save_state(stt)
                        st.rerun()

    with tab3:
        n = st.text_input("추가할 티커", placeholder="예: CRDO").strip().upper()
        if n and st.button("추가", use_container_width=True):
            if n in watch:
                st.warning("이미 있습니다.")
            else:
                stt["tickers"] = watch + [n]
                save_state(stt)
                st.rerun()
        st.divider()
        if not watch:
            st.caption("등록된 종목이 없습니다.")
        else:
            st.caption("진입밴드: 225-250 처럼 입력 (단일값 158도 가능, 비우면 해제)")
        changed = False
        for t in watch:
            c1, c2, c3 = st.columns([2, 3, 1])
            c1.write(f"**{t}**")
            cur = band_text(bands[t]) if t in bands else ""
            raw = c2.text_input("밴드", value=cur, key=f"band_{t}",
                                placeholder="예: 225-250",
                                label_visibility="collapsed")
            nb = parse_band(raw)
            if raw.strip() and nb is None:
                c2.caption("⚠ 형식 오류 — 예: 225-250")
            elif nb != bands.get(t):
                if nb is None:
                    bands.pop(t, None)
                else:
                    bands[t] = nb
                changed = True
            if c3.button("삭제", key=f"d{t}"):
                stt["tickers"] = [x for x in watch if x != t]
                bands.pop(t, None)
                save_state(stt)
                st.rerun()
        if changed:
            save_state(stt)
        # 백업/복원은 목록이 비어 있을 때도 보여야 한다.
        # (재시작으로 목록이 날아갔을 때 복원해야 하는데,
        #  비었다고 복원 칸을 숨기면 복구할 방법이 없어진다)
        st.divider()
        if watch:
            st.caption("백업 (앱이 재시작되면 목록이 초기화될 수 있으니 "
                       "가끔 아래 내용을 복사해 두세요)")
            st.code(json.dumps(stt, ensure_ascii=False))
        else:
            st.caption("목록이 비어 있습니다. 복사해 둔 백업이 있으면 "
                       "아래에서 복원하세요.")
        with st.expander("백업 복원", expanded=not watch):
            rb = st.text_area("복사해 둔 백업 붙여넣기", height=68,
                              placeholder='{"tickers": ["MU","ALAB"], "bands": {}}')
            if st.button("복원", use_container_width=True):
                try:
                    raw = json.loads(rb)
                    if isinstance(raw, list):
                        stt2 = {"tickers": raw, "bands": {}}
                    else:
                        bad_n = 0
                        rb_ = raw.get("bands")
                        if isinstance(rb_, dict):
                            bad_n = len(rb_) - len(clean_bands(rb_))
                        stt2 = {"tickers": raw.get("tickers", []),
                                "bands": clean_bands(rb_)}
                        if bad_n:
                            st.warning(f"밴드 {bad_n}개는 형식이 맞지 않아 "
                                       f"빼고 복원했습니다. "
                                       f'형식: {{"MU": [225, 250]}}')
                    save_state(stt2)
                    st.rerun()
                except Exception:
                    st.error("붙여넣은 내용이 올바른 백업 형식이 아닙니다.")


if gate():
    main()
