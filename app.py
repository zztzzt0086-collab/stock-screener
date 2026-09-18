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
                  score_damo, damo_verdict, yearly_series, implied_growth, RETIRED,
                  value_verdict, year_end_prices,
                  YEARLY_AXES, YEARLY_TRI,
                  market_snapshot, vix_mood,
                  USD_KRW, chart_data,
                  money,
                  CHARCOAL, ORANGE, AMBER, SLATE, MUTED, BLUE, GRAY,
                  score_ten, score_lt, fetch, won, pctile,
                  ten_verdict, lt_verdict, dday, footprint, fp_verdict)

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
.nm {{font-size:.72rem;color:{MUTED};}}
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
.mk {{color:{MUTED};}} .mv {{color:{CHARCOAL};font-weight:600;}}
.sect {{font-size:.7rem;letter-spacing:.1em;color:{ORANGE};
       font-weight:700;margin:18px 0 7px;}}
.note {{font-size:.72rem;color:{MUTED};line-height:1.5;}}
.stButton>button {{border-radius:7px;font-weight:600;}}
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
            stt = {"tickers": raw, "bands": {}, "buys": {}}
        else:
            stt = {"tickers": raw.get("tickers", []),
                   "bands": raw.get("bands", {}),
                   "buys": raw.get("buys", {})}
    except Exception:
        stt = {"tickers": [], "bands": {}, "buys": {}}
    st.session_state["wstate"] = stt
    return stt


def save_state(stt):
    st.session_state["wstate"] = stt
    try:
        with open(WATCHFILE, "w", encoding="utf-8") as f:
            json.dump(stt, f, ensure_ascii=False)
    except Exception:
        pass


def parse_band(s):
    """'225-250' / '225~250' / '158' → [lo, hi], 실패 시 None"""
    s = (s or "").replace("~", "-").replace(",", "").strip()
    if not s:
        return None
    parts = [p.strip() for p in s.split("-") if p.strip()]
    try:
        if len(parts) == 1:
            v = float(parts[0])
            return [v, v]
        lo, hi = float(parts[0]), float(parts[1])
        if lo > hi:
            lo, hi = hi, lo
        return [lo, hi]
    except Exception:
        return None


def band_text(band):
    lo, hi = band
    f = lambda v: f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"
    return f(lo) if lo == hi else f"{f(lo)}~{f(hi)}"


def band_status(price, band):
    """(라벨, 색, 밴드 안 여부) — 밴드 미설정/가격 없음이면 None"""
    if not band or not price:
        return None
    lo, hi = band
    if lo <= price <= hi:
        return "◆ 밴드 안", ORANGE, True
    if price > hi:
        d = (price / hi - 1) * 100
        if d <= 5:
            return f"밴드까지 -{d:.1f}%", AMBER, False
        return f"밴드 위 +{d:.1f}%", MUTED, False
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
    return (f'<div class="bandline"><span style="color:{MUTED}">수급 동향</span>'
            f'<span style="color:{CHARCOAL};font-weight:700">{fp["score"]}</span>'
            f'<span style="color:{col};font-weight:700"> · {lab}</span></div>')



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


# ═════════════════════════════════════════════════════════════
# 요즘 회사가 어떤가 — 연도별 오각형 + 표
#
#   기존 두 채점과 별개다. 재무제표만으로 계산되는 다섯 가지를
#   연도별로 본다. 주가·시총이 필요한 항목은 과거 값이 없어 뺐다.
#
#   배경(옅은 회색) = 각 축의 역대 최고
#   앞(주황)        = 선택한 연도
#   → "전성기 대비 지금 어디쯤인가" 가 보인다.
# ═════════════════════════════════════════════════════════════


def market_bar():
    """화면 맨 위 시장 한 줄. 채점과 무관한 참고용."""
    try:
        ms = market_snapshot()
    except Exception:
        return
    if not ms:
        return
    GREEN, RED, GRAY = "#16A34A", "#DC2626", MUTED
    cells = []
    for m in ms:
        lab, val = m["label"], m["value"]
        col = GRAY
        extra = ""
        if lab == "VIX":
            mood = vix_mood(m["raw"])
            col = (GREEN if m["raw"] < 20 else
                   "#D97706" if m["raw"] < 30 else RED)
            extra = f' <span style="color:{GRAY};font-weight:400">{mood}</span>'
        elif lab in ("나스닥", "S&P"):
            col = GREEN if (m.get("chg") or 0) >= 0 else RED
        else:
            col = CHARCOAL
        cells.append(
            f'<span style="white-space:nowrap">'
            f'<span style="color:{GRAY};font-size:.68rem">{lab}</span> '
            f'<span style="color:{col};font-weight:700;font-size:.72rem">'
            f'{val}</span>{extra}</span>')
    st.markdown(
        '<div style="display:flex;gap:12px;flex-wrap:wrap;'
        'padding:6px 0 10px;border-bottom:1px solid #EEE;margin-bottom:10px">'
        + "".join(cells) + "</div>", unsafe_allow_html=True)


def yearly_svg(ys, idx):
    """삼각형. 계산에 쓰는 항목이 적어 덜 틀어지는 세 가지만 그린다.
    ROIC 와 FCF 는 가정이 들어가거나 항목이 많이 필요해 표에만 둔다."""
    import math
    labels = YEARLY_TRI
    cur = [ys["score"][k][idx] for k in labels]
    best = [ys["best"][k] for k in labels]

    W, H, R = 300, 250, 78
    cx, cy = W / 2, H / 2 - 4
    n = len(labels)

    def pt(i, r):
        ang = -math.pi / 2 + 2 * math.pi * i / n
        return cx + r * math.cos(ang), cy + r * math.sin(ang)

    parts = []
    # 눈금
    for f in (0.33, 0.66, 1.0):
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in
                       (pt(i, R * f) for i in range(n)))
        parts.append(f'<polygon points="{pts}" fill="none" '
                     f'stroke="#CBD5E1" stroke-width="1"/>')
    # 축선
    for i in range(n):
        x, y = pt(i, R)
        parts.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" '
                     f'stroke="#CBD5E1" stroke-width="1"/>')

    # 배경 = 역대 최고
    if all(v is not None for v in best):
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in
                       (pt(i, R * best[i] / 100) for i in range(n)))
        parts.append(f'<polygon points="{pts}" fill="#94A3B8" '
                     f'fill-opacity="0.38" stroke=MUTED stroke-width="1"/>')

    # 앞 = 선택 연도
    if all(v is not None for v in cur):
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in
                       (pt(i, R * cur[i] / 100) for i in range(n)))
        parts.append(f'<polygon points="{pts}" fill="{ORANGE}" '
                     f'fill-opacity="0.22" stroke="{ORANGE}" stroke-width="2"/>')
        for i in range(n):
            x, y = pt(i, R * cur[i] / 100)
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.6" '
                         f'fill="{ORANGE}"/>')

    # 라벨 + 실제 값
    for i, lab in enumerate(labels):
        x, y = pt(i, R + 26)
        anchor = "middle"
        if x < cx - 8:
            anchor = "end"
        elif x > cx + 8:
            anchor = "start"
        v = ys["raw"][lab][idx]
        parts.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="10" '
                     f'fill=MUTED text-anchor="{anchor}">{lab}</text>')
        parts.append(f'<text x="{x:.1f}" y="{y + 12:.1f}" font-size="11" '
                     f'font-weight="700" fill="{CHARCOAL}" '
                     f'text-anchor="{anchor}">'
                     f'{"-" if v is None else f"{v:.0f}%"}</text>')

    return (f'<div style="margin:4px 0"><svg viewBox="0 0 {W} {H}" '
            f'width="100%" style="max-width:340px;display:block;margin:0 auto">'
            + "".join(parts) + "</svg></div>")


def yearly_section(d):
    ys = yearly_series(d)
    if not ys or len(ys["years"]) < 2:
        return

    st.markdown('<div class="sect">요즘 회사가 어떤가</div>',
                unsafe_allow_html=True)

    years = ys["years"]
    key = f"yr_{d['ticker']}"
    pick = st.radio("연도", years, index=len(years) - 1, horizontal=True,
                    key=key, label_visibility="collapsed")
    idx = years.index(pick)

    st.markdown(yearly_svg(ys, idx), unsafe_allow_html=True)

    # 표
    head = "".join(f'<th style="text-align:right;padding:4px 6px;'
                   f'font-size:.72rem;color:{MUTED};font-weight:600">{y}</th>'
                   for y in years) + (
        '<th style="text-align:left;padding:4px 8px;font-size:.64rem;'
        'color:{MUTED};font-weight:600">쓰는 값</th>')
    rows = []
    for lab, what, src in YEARLY_AXES:
        cells = "".join(
            f'<td style="text-align:right;padding:4px 6px;font-size:.78rem;'
            f'font-weight:{700 if i == idx else 400};'
            f'color:{CHARCOAL if i == idx else MUTED}">'
            f'{"-" if v is None else f"{v:.0f}%"}</td>'
            for i, v in enumerate(ys["raw"][lab]))
        tri = lab in YEARLY_TRI
        dot = ("△ " if tri else "")
        rows.append(
            f'<tr><td style="padding:4px 6px;font-size:.78rem;'
            f'font-weight:{700 if tri else 400}">{dot}{lab}</td>'
            f'<td style="padding:4px 6px;font-size:.7rem;color:{MUTED}">'
            f'{what}</td>{cells}'
            f'<td style="padding:4px 8px;font-size:.64rem;color:{MUTED};'
            f'text-align:left;white-space:nowrap">{src}</td></tr>')
    st.markdown(
        f'<table style="width:100%;border-collapse:collapse;margin-top:2px">'
        f'<tr><th></th><th></th>{head}</tr>' + "".join(rows) + "</table>",
        unsafe_allow_html=True)

    st.markdown('<p class="note">△ 세 가지만 그림으로 그립니다. '
                '계산에 쓰는 항목이 적어 덜 틀어지기 때문입니다. '
                'ROIC 는 세율을 가정하고 항목도 다섯 개가 필요해 표에만 둡니다.<br>'
                '배경은 각 축의 역대 최고, 주황은 고른 연도입니다. '
                '기존 채점(성장 잠재력·장기 보유)과 별개입니다.</p>',
                unsafe_allow_html=True)


def damo_section(d):
    rc = d.get("roic")
    re_ = d.get("reinv_eff")
    revs = d.get("revs") or []
    margins = d.get("margins") or []
    if rc is None and re_ is None and len(revs) < 2:
        return

    cur = d.get("fin_currency") or d.get("currency", "USD")
    GREEN, RED, GRAY = "#16A34A", "#DC2626", MUTED

    # ── 값만 보여 준다 ──
    # 100점 만점 점수와 "가치 창출형" 같은 판정은 배점 근거가
    # 검증되지 않아 화면에서 뺐다. 오각형도 뺐다. 배점이 다른 항목을
    # 같은 축 길이로 그리면 35점 항목이 10점 항목보다 짧아 보일 수 있다.
    # 계산은 계속 하고 history 에 저장되므로 나중에 검증할 수 있다.
    items = score_damo(d)
    st.markdown('<div class="sect">다모다란 관점</div>', unsafe_allow_html=True)
    if items:
        st.markdown("".join(
            f'<div class="metric"><span class="mk">{k}</span>'
            f'<span class="mv">{txt}</span></div>'
            for k, _sc, txt in items), unsafe_allow_html=True)
        st.markdown('<p class="note">기존 채점(성장 잠재력·장기 보유)과 '
                    '별개입니다. "지금 재무가 좋은가" 가 아니라 '
                    '"자본을 굴려 가치를 만들고 있는가" 를 봅니다. '
                    '점수는 매기지 않고 값만 보여 줍니다.</p>',
                    unsafe_allow_html=True)

    with st.expander("세부 보기"):
        wacc = st.slider("자본비용 가정 (%)", 5.0, 15.0, 9.0, 0.5,
                         key=f"wacc_{d['ticker']}",
                         help="보통 8~10%. 위험한 회사일수록 높게 잡는다")

        blocks = []

        # 1. 가치 창출
        rows = []
        if rc is not None:
            gap = rc - wacc
            col = GREEN if gap > 0 else RED
            rc_txt = f"{rc:.1f}%"
            if d.get("roic_note"):
                rc_txt += f"  ※{d['roic_note']}"
            rows += [("ROIC", rc_txt, None),
                     ("자본비용 가정", f"{wacc:.1f}%", None),
                     ("초과수익",
                      f'<span style="color:{col};font-weight:700">{gap:+.1f}%p</span>'
                      f' · {"가치 창출" if gap > 0 else "가치 파괴"}', None)]
        else:
            rows.append(("ROIC", "계산 불가 (데이터 부족)", GRAY))
        blocks.append(("1. 가치를 만들고 있나", rows,
                       "ROE 는 빚을 많이 쓰면 부풀려지지만 ROIC 는 그렇지 않습니다. "
                       "자본비용보다 높아야 가치를 만드는 것입니다."))

        # 2. 성장의 대가
        rows = []
        if re_ is not None:
            j = ("효율 높음" if re_ >= 2 else "보통" if re_ >= 0.5 else
                 "투자 회수 전" if re_ >= 0 else "매출 감소 중")
            v_ = f"{re_:.2f}배 · {j}"
            if d.get("reinv_note"):
                v_ += f"  ※{d['reinv_note']}"
            rows.append(("재투자 효율", v_, None))
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
            tr = margins[-1] - margins[0]
            rows.append(("추세", f"{tr:+.0f}%p · "
                         f'{"개선 중" if tr > 2 else "악화 중" if tr < -2 else "횡보"}',
                         None))
        if rows:
            blocks.append(("3. 어떻게 크고 있나", rows, None))

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


def price_chart(t, currency="USD"):
    """1년 일봉 캔들 + 20/50MA + 기간별 수익률."""
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
    if hi <= lo:
        return
    span = hi - lo
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
                     f'y2="{yy:.1f}" stroke="#CBD5E1" stroke-width="1"/>')
        fmt = f"{v:,.0f}" if currency == "KRW" else f"{v:,.1f}"
        parts.append(f'<text x="{PAD_L+iw+6:.1f}" y="{yy+3.5:.1f}" font-size="9" '
                     f'fill=MUTED>{fmt}</text>')

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
    for key, col, wdt in (("ma20", ORANGE, 1.3), ("ma50", MUTED, 1.1)):
        pts = [f"{x(i):.1f},{y(r[key]):.1f}" for i, r in enumerate(rows)
               if r.get(key)]
        if len(pts) > 2:
            parts.append(f'<polyline points="{" ".join(pts)}" fill="none" '
                         f'stroke="{col}" stroke-width="{wdt}" opacity="0.85"/>')

    # 날짜 라벨 (양 끝 + 가운데)
    for i in (0, n // 2, n - 1):
        anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
        parts.append(f'<text x="{x(i):.1f}" y="{H-4}" font-size="9" '
                     f'fill=MUTED text-anchor="{anchor}">'
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
            txt, col = "-", MUTED
        else:
            txt = f"{v:+.1f}%"
            col = UP if v > 0 else (DN if v < 0 else MUTED)
        cells.append(
            f'<div style="flex:1;text-align:center">'
            f'<div style="font-size:.7rem;color:{MUTED}">{label}</div>'
            f'<div style="font-size:.85rem;font-weight:700;color:{col}">{txt}</div>'
            f'</div>')
    st.markdown(f'<div style="display:flex;gap:2px;margin-bottom:6px">'
                + "".join(cells) + "</div>", unsafe_allow_html=True)
    st.markdown('<p class="note">주황 20일선 · 회색 50일선 · '
                '빨강 상승 · 파랑 하락</p>', unsafe_allow_html=True)


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
      <span style="font-size:.9rem;font-weight:700;color:{CHARCOAL}">{fp['score']}/100</span></div>
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





def radar_svg(items, mx, mode, center_score, center_col, axes=None):
    """items=[(k,s,v)], mx=만점dict → 5축 퍼센트 레이더 SVG 문자열.
    axes 를 직접 주면 그것을 쓰고, 없으면 mode 로 고른다."""
    # 채점 못 한 항목은 축에서 뺀다 (0 으로 치면 축이 잘못 낮아진다)
    got = {k: s for k, s, _ in items if s is not None}
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
                 f'stroke="#E4E4E7" stroke-width="1"/>')
    for i in range(5):
        x, y = pt(i, R)
        grid += (f'<line x1="{CX}" y1="{CY}" x2="{x:.1f}" y2="{y:.1f}" '
                 f'stroke="#CBD5E1" stroke-width="1"/>')

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
                 f'font-size="11" fill=MUTED>{label}</text>'
                 f'<text x="{lx:.1f}" y="{ly + 15:.1f}" text-anchor="middle" '
                 f'font-size="13" font-weight="700" fill="{CHARCOAL}">{num}</text>')

    center = (f'<text x="{CX}" y="{CY + 9:.1f}" text-anchor="middle" font-size="26" '
              f'font-weight="800" fill="{center_col}">{center_score:.0f}</text>')

    return (f'<div style="display:flex;justify-content:center;margin:4px 0 10px">'
            f'<svg viewBox="0 0 {W} {H}" width="100%" style="max-width:320px">'
            f'{grid}{poly}{dots}{center}{labs}</svg></div>')


def card(d, mode, band=None, buy=None):
    """대시보드 카드. 성장·장기 두 점수를 같이 보여 준다.
    전에는 라디오로 하나만 골라 보여 줬는데, 두 잣대가 자주 갈려서
    (구글 성장 56% / 장기 88%) 한쪽만 보면 오해하기 쉽다."""
    both = []
    for mk, label, sf, mxx, vf in (("ten", "성장", score_ten, TEN_MAX, ten_verdict),
                                   ("lt", "장기", score_lt, LT_MAX, lt_verdict)):
        it = sf(d)
        g_, av_, p_ = pctile(it, mxx)
        tg_, cl_ = vf(p_)
        both.append((label, g_, av_, p_, tg_, cl_))

    # 고른 관점을 위에 둔다
    if mode == "lt":
        both = both[::-1]
    p = both[0][3]
    col = both[0][5]

    ch = d["chg"]
    cls = "up" if (ch or 0) >= 0 else "dn"
    chtxt = f"{ch:+.2f}%" if ch is not None else ""
    px = (f"{d['price']:,.0f}" if d.get("currency") == "KRW"
          else f"{d['price']:,.2f}") if d["price"] else "-"

    # 내 매수가 대비 수익률. % 만 보여 준다.
    # 금액은 일부러 안 낸다 — 마이너스 금액을 보면 판단이 흔들린다.
    buyline = ""
    if buy and d.get("price"):
        try:
            r = (float(d["price"]) / float(buy) - 1) * 100
            bcol = "#16A34A" if r >= 0 else "#DC2626"
            buyline = (f'<div class="bandline">'
                       f'<span style="color:{MUTED}">내 매수가 {float(buy):,.2f}</span>'
                       f'<span style="color:{bcol};font-weight:700">'
                       f'{r:+.1f}%</span></div>')
        except Exception:
            pass

    # 밸류 판정 한 줄. ★검증 안 된 값이라 그렇게 적는다.★
    valline = ""
    try:
        g0, _, _ = implied_growth(d)
        vv0 = value_verdict(d, year_end_prices(d["ticker"]), g0)
        if vv0 and vv0.get("label") and vv0["label"] != "모름":
            vc0 = {"저평가 쪽": "#16A34A",
                   "고평가 쪽": "#DC2626"}.get(vv0["label"], MUTED)
            valline = (f'<div class="bandline">'
                       f'<span style="color:{MUTED}">밸류 '
                       f'<span style="font-size:.64rem">(검증 안 됨)</span></span>'
                       f'<span style="color:{vc0};font-weight:700">'
                       f'{vv0["label"]}</span></div>')
    except Exception:
        pass

    bandline = ""
    bs = band_status(d["price"], band)
    if bs:
        label, bcol, _ = bs
        bandline = (f'<div class="bandline">'
                    f'<span style="color:{MUTED}">진입밴드 {band_text(band)}</span>'
                    f'<span style="color:{bcol};font-weight:700">{label}</span></div>')

    scorelines = "".join(
        f'<div style="display:flex;justify-content:space-between;'
        f'align-items:center;margin-top:{11 if i == 0 else 9}px">'
        f'<span style="font-size:.7rem;color:{MUTED};min-width:26px">{lab}</span>'
        f'<span class="badge" style="background:{cl_};margin-left:6px">{tg_}</span>'
        f'<span style="flex:1"></span>'
        f'<span style="font-size:.78rem;color:{MUTED}">{g_}/{av_} '
        f'<b style="color:{CHARCOAL}">{p_:.0f}%</b></span></div>'
        f'<div class="bar-bg" style="margin-top:5px">'
        f'<div class="bar-fl" style="width:{p_:.0f}%;background:{cl_}"></div></div>'
        for i, (lab, g_, av_, p_, tg_, cl_) in enumerate(both))

    st.markdown(f"""<div class="card">
      <div class="card-hd">
        <div><span class="tkr">{d['ticker']}</span>
             <div class="nm">{(d['name'] or '')[:30]}</div></div>
        <div style="text-align:right">
          <div class="px">{px}</div><div class="{cls}">{chtxt}</div></div>
      </div>
      {scorelines}
      {valline}{buyline}{bandline}{fp_line(footprint(d['ticker']))}
    </div>""", unsafe_allow_html=True)


def detail(d, band=None, buy=None):
    st.markdown(f"### {d['ticker']}")
    st.caption(f"{d['name']}  ·  {d['sector'] or ''}")

    if buy and d.get("price"):
        try:
            r_ = (float(d["price"]) / float(buy) - 1) * 100
            c_ = "#16A34A" if r_ >= 0 else "#DC2626"
            st.markdown(
                f'<div style="display:flex;justify-content:space-between;'
                f'align-items:center;background:#F9FAFB;border-radius:6px;'
                f'padding:7px 11px;margin:6px 0 2px">'
                f'<span style="font-size:.74rem;color:{MUTED}">'
                f'내 매수가 {float(buy):,.2f}</span>'
                f'<span style="font-size:.95rem;font-weight:700;color:{c_}">'
                f'{r_:+.1f}%</span></div>', unsafe_allow_html=True)
        except Exception:
            pass

    c1, c2, c3 = st.columns(3)
    # 원화는 소수점을 안 붙인다 (55,000.00 처럼 나오던 것)
    _px = ((f"{d['price']:,.0f}" if d.get("currency") == "KRW"
            else f"{d['price']:,.2f}") if d["price"] else "-")
    c1.metric("현재가", _px,
              f"{d['chg']:+.2f}%" if d["chg"] is not None else None)
    c2.metric("시총", won(d["mcap_krw"]))
    e, dd_ = dday(d["earnings"])
    c3.metric("실적발표", f"D{-dd_:+d}" if dd_ is not None and -30 < dd_ < 300 else "-")

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
          <span style="font-size:.9rem;font-weight:700;color:{CHARCOAL}">{p:.0f}%</span></div>
          <div class="bar-bg"><div class="bar-fl"
          style="width:{p:.0f}%;background:{col}"></div></div>""",
          unsafe_allow_html=True)
        st.markdown(radar_svg(items, mx, mode, p, col), unsafe_allow_html=True)
        rows = "".join(
            f'<div class="metric"><span class="mk"'
            + (' style="color:{MUTED}"' if s is None else '') + f'>{k}</span>'
            f'<span class="mv"'
            + (' style="color:{MUTED};font-weight:400"' if s is None else '') + f'>{v} '
            f'<span style="color:{MUTED};font-weight:400">'
            f'{"-" if s is None else s}/{mx[k]}</span></span></div>'
            for k, s, v in items)
        st.markdown(rows, unsafe_allow_html=True)
        if mode == "ten" and RETIRED:
            st.markdown('<p class="note">쉬는 항목 ' + str(len(RETIRED)) + '개 — '
                        + ' · '.join(f'<b>{k}</b> {why}'
                                     for k, why in RETIRED.items())
                        + '</p>', unsafe_allow_html=True)

    price_chart(d['ticker'], d.get('currency', 'USD'))

    fp_section(footprint(d['ticker']))

    # ── 가격이 무엇을 기대하나 ──
    #   "비싸다 / 싸다" 는 말하지 않는다. 그건 우리가 정할 수 있는 게 아니다.
    #   숫자를 나란히 놓고 판단은 사람이 한다.
    vrows = []
    if d.get("per"):
        vrows.append(("PER", f"{d['per']:.1f}"))
    if d.get("peg") is not None:
        note = ""
        if 0 < d["peg"] < 0.2:
            note = "  (0.2 미만 — 적자→흑자 착시일 수 있음)"
        elif d["peg"] < 0:
            note = "  (음수 — 적자·역성장)"
        vrows.append(("PEG", f"{d['peg']:.2f}{note}"))
    try:
        g_, m_, note_ = implied_growth(d)
    except Exception:
        g_ = m_ = note_ = None
    if g_ is not None:
        vrows.append(("지금 주가가 기대하는 성장률",
                      f"연 {g_*100:.0f}%  (10년, 목표 이익률 {m_*100:.0f}% 가정)"))
        if d.get("cagr") is not None:
            gap = d["cagr"] - g_ * 100
            word = (f"기대치보다 {abs(gap):.0f}%p 낮다" if gap < 0
                    else f"기대치보다 {gap:.0f}%p 높다")
            vrows.append(("실제 장기 CAGR", f"{d['cagr']:.1f}%  ·  {word}"))
    elif note_:
        vrows.append(("역산", note_))

    # 밸류 판정 (저평가 / 적정 / 고평가 / 모름)
    #   ★ 검증되지 않았다. 화면에 그렇게 적는다.
    #     경계도 임의값이고, 밴드 표본이 3~4개뿐이다.
    vv = None
    try:
        vv = value_verdict(d, year_end_prices(d["ticker"]),
                           g_ if g_ is not None else None)
    except Exception:
        pass

    if vrows:
        st.markdown('<div class="sect">가격이 기대하는 것</div>',
                    unsafe_allow_html=True)
        if vv and vv.get("label"):
            vc = {"저평가 쪽": "#16A34A", "고평가 쪽": "#DC2626",
                  "모름": MUTED}.get(vv["label"], MUTED)
            bn = vv.get("band_n") or 0
            st.markdown(
                f'<div style="display:flex;justify-content:space-between;'
                f'align-items:center;margin-bottom:8px">'
                f'<span class="badge" style="background:{vc}">'
                f'{vv["label"]}</span>'
                f'<span style="font-size:.68rem;color:{MUTED}">'
                f'검증 안 됨 · 밴드 표본 {bn}개</span></div>',
                unsafe_allow_html=True)
            if vv.get("reasons"):
                st.markdown("".join(
                    f'<div style="font-size:.7rem;color:{MUTED};'
                    f'padding:2px 0">· {r}</div>' for r in vv["reasons"]),
                    unsafe_allow_html=True)
        st.markdown("".join(
            f'<div class="metric"><span class="mk">{k}</span>'
            f'<span class="mv">{v}</span></div>' for k, v in vrows),
            unsafe_allow_html=True)
        st.markdown('<p class="note"><b>이 판정은 검증되지 않았습니다.</b> '
                    '경계(하위 30% 등)는 임의로 정한 값이고, 자기 이력 밴드는 '
                    '야후가 연간 재무를 4년치만 줘서 표본이 3~4개뿐입니다. '
                    '6개월 뒤 verify 로 쓸모를 확인할 예정입니다.<br>'
                    '역산은 재투자 40%·세율 21%·자본비용 9%·영구성장 3% 를 '
                    '가정한 값이며, 이 가정들도 임의로 정한 것입니다.</p>',
                    unsafe_allow_html=True)

    st.markdown('<div class="sect">시장 지표</div>', unsafe_allow_html=True)
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

    yearly_section(d)

    damo_section(d)

    st.markdown('<p class="note">체크리스트일 뿐 추천이 아닙니다. '
                '자동 수집값은 누락·오류가 있을 수 있으니 최종 판단 전 '
                '실적발표 원문을 확인하세요.</p>', unsafe_allow_html=True)


def main():
    stt = load_state()
    watch, bands = stt["tickers"], stt["bands"]
    buys = stt.setdefault("buys", {})     # 내 매수가 (수익률 표시용)
    market_bar()
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
                mode = st.radio("관점", ["성장", "장기"], horizontal=True,
                                label_visibility="collapsed")
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
                            f'{px_:,.2f} (밴드 {band_text(bd_)}) · 분할매수 계획 확인</div>',
                            unsafe_allow_html=True)

            # 실적 D-day 배너 (차콜)
            for tk_, e, dd_ in sorted(alerts, key=lambda x: x[2]):
                st.markdown(f'<div class="dday">◆ <b>{tk_}</b> 실적발표 '
                            f'{e:%m/%d} · <b>D-{dd_}</b> — 발표 후 다시 확인</div>',
                            unsafe_allow_html=True)

            # 수급이 한쪽으로 크게 치우친 종목 표시.
            # 백테스트 결과 수급 점수는 이후 수익률과 상관이 없었다(-0.03).
            # 그래서 "사라/팔라"가 아니라 "왜 이런지 확인해 보라"는 안내로 쓴다.
            # 매수 우위만이 아니라 매도 우위도 똑같이 보여 준다.
            for d in data:
                fp_ = footprint(d["ticker"])
                if not fp_:
                    continue
                sc_ = fp_["score"]
                if sc_ >= 75:
                    st.markdown(f'<div class="dday">◆ <b>{d["ticker"]}</b> 수급 {sc_} '
                                f'· 6개월간 매수 우위 — 이유 확인 필요</div>',
                                unsafe_allow_html=True)
                elif sc_ <= 25:
                    st.markdown(f'<div class="dday">◆ <b>{d["ticker"]}</b> 수급 {sc_} '
                                f'· 6개월간 매도 우위 — 이유 확인 필요</div>',
                                unsafe_allow_html=True)

            for d in data:
                t_ = d["ticker"]
                card(d, m, bands.get(t_), buys.get(t_))

                # ★ 매수가는 여기서 넣는다.
                #   관심종목 탭에 두었더니 대시보드가 먼저 그려져
                #   입력이 한 박자 늦게 반영됐다.
                cb1, cb2 = st.columns([1, 1])
                braw = cb1.text_input(
                    "매수가", value=(f"{buys[t_]:g}" if t_ in buys else ""),
                    key=f"dbuy_{t_}", placeholder="내 매수가",
                    label_visibility="collapsed")
                bs_ = braw.strip().replace(",", "")
                if not bs_ and t_ in buys:
                    buys.pop(t_, None)
                    save_state(stt)
                    st.rerun()
                elif bs_:
                    try:
                        bv = float(bs_)
                        if bv > 0 and buys.get(t_) != bv:
                            buys[t_] = bv
                            save_state(stt)
                            st.rerun()
                    except ValueError:
                        cb1.caption("숫자만")

                if cb2.button("상세", key=f"b{t_}", use_container_width=True):
                    st.session_state["sel"] = t_
                    st.rerun()

            if st.session_state.get("sel"):
                st.divider()
                dd2 = fetch(st.session_state["sel"])
                if dd2:
                    detail(dd2, bands.get(st.session_state["sel"]),
                           buys.get(st.session_state["sel"]))
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
                detail(d, bands.get(t), buys.get(t))
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
            st.caption("진입밴드는 225-250 처럼 입력 (단일값 158도 가능, "
                       "비우면 해제). 내 매수가는 대시보드 카드 아래에서 "
                       "넣습니다.")
        changed = False
        for t in watch:
            c1, c2, c3, c4 = st.columns([2, 3, 2, 1])
            c1.write(f"**{t}**")
            cur = band_text(bands[t]) if t in bands else ""
            raw = c2.text_input("밴드", value=cur, key=f"band_{t}",
                                placeholder="진입밴드 225-250",
                                label_visibility="collapsed")
            nb = parse_band(raw)
            if raw.strip() and nb is None:
                c2.caption("형식 오류 — 예: 225-250")
            elif nb != bands.get(t):
                if nb is None:
                    bands.pop(t, None)
                else:
                    bands[t] = nb
                changed = True

            # 매수가는 대시보드 카드 아래에서 넣는다.

            if c4.button("삭제", key=f"d{t}"):
                stt["tickers"] = [x for x in watch if x != t]
                bands.pop(t, None)
                buys.pop(t, None)
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
                        stt2 = {"tickers": raw, "bands": {}, "buys": {}}
                    else:
                        stt2 = {"tickers": raw.get("tickers", []),
                                "bands": raw.get("bands", {}),
                                "buys": raw.get("buys", {})}
                    save_state(stt2)
                    st.rerun()
                except Exception:
                    st.error("붙여넣은 내용이 올바른 백업 형식이 아닙니다.")


if gate():
    main()
