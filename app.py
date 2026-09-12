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

from core import (TEN_MAX, LT_MAX, AXES_TEN, AXES_LT, USD_KRW, chart_data,
                  CHARCOAL, ORANGE, AMBER,
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
            stt = {"tickers": raw.get("tickers", []),
                   "bands": raw.get("bands", {})}
    except Exception:
        stt = {"tickers": [], "bands": {}}
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

    rows = [("상승/하락 거래량비 (20일)", f"{fp['updn']:.2f}"),
            ("대량거래일 (양봉/음봉)", f"{fp['spike_up']} / {fp['spike_dn']}"),
            ("OBV 20일 변화", f"{fp['obv_chg']*100:+.0f}% (가격 {fp['px_chg']*100:+.1f}%)"),
            ("6개월 고점 대비", f"{fp['off_hi']*100:+.1f}%"),
            ("20MA / 50MA", f"{fp['ma20']:,.2f} / {fp['ma50']:,.2f}")]
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





def radar_svg(items, mx, mode, center_score, center_col):
    """items=[(k,s,v)], mx=만점dict → 5축 퍼센트 레이더 SVG 문자열."""
    got = {k: s for k, s, _ in items}
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


def card(d, mode, band=None):
    items = score_ten(d) if mode == "ten" else score_lt(d)
    mx = TEN_MAX if mode == "ten" else LT_MAX
    got, avail, p = pctile(items, mx)
    tag, col = (ten_verdict(p) if mode == "ten" else lt_verdict(p))
    ch = d["chg"]
    cls = "up" if (ch or 0) >= 0 else "dn"
    chtxt = f"{ch:+.2f}%" if ch is not None else ""
    px = f"{d['price']:,.2f}" if d["price"] else "-"

    bandline = ""
    bs = band_status(d["price"], band)
    if bs:
        label, bcol, _ = bs
        bandline = (f'<div class="bandline">'
                    f'<span style="color:#9CA3AF">진입밴드 {band_text(band)}</span>'
                    f'<span style="color:{bcol};font-weight:700">{label}</span></div>')

    st.markdown(f"""<div class="card">
      <div class="card-hd">
        <div><span class="tkr">{d['ticker']}</span>
             <div class="nm">{(d['name'] or '')[:30]}</div></div>
        <div style="text-align:right">
          <div class="px">{px}</div><div class="{cls}">{chtxt}</div></div>
      </div>
      <div style="display:flex;justify-content:space-between;
                  align-items:center;margin-top:11px">
        <span class="badge" style="background:{col}">{tag}</span>
        <span style="font-size:.8rem;color:#6B7280">{got}/{avail}
              <b style="color:{col}">{p:.0f}%</b></span>
      </div>
      <div class="bar-bg"><div class="bar-fl"
           style="width:{p:.0f}%;background:{col}"></div></div>
      {bandline}{fp_line(footprint(d['ticker']))}
    </div>""", unsafe_allow_html=True)


def detail(d, band=None):
    st.markdown(f"### {d['ticker']}")
    st.caption(f"{d['name']}  ·  {d['sector'] or ''}")

    c1, c2, c3 = st.columns(3)
    c1.metric("현재가", f"{d['price']:,.2f}" if d["price"] else "-",
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
          <span style="font-size:.9rem;font-weight:700;color:{col}">{p:.0f}%</span></div>
          <div class="bar-bg"><div class="bar-fl"
          style="width:{p:.0f}%;background:{col}"></div></div>""",
          unsafe_allow_html=True)
        st.markdown(radar_svg(items, mx, mode, p, col), unsafe_allow_html=True)
        rows = "".join(
            f'<div class="metric"><span class="mk">{k}</span>'
            f'<span class="mv">{v} <span style="color:#9CA3AF;font-weight:400">'
            f'{s}/{mx[k]}</span></span></div>'
            for k, s, v in items)
        st.markdown(rows, unsafe_allow_html=True)

    price_chart(d['ticker'], d.get('currency', 'USD'))

    fp_section(footprint(d['ticker']))

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

    st.markdown('<p class="note">체크리스트지 추천이 아닙니다. '
                '자동 수집값은 누락·오류가 있을 수 있으니 최종 판단 전 '
                '실적발표 원문을 확인하세요.</p>', unsafe_allow_html=True)


def main():
    stt = load_state()
    watch, bands = stt["tickers"], stt["bands"]
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
                card(d, m, bands.get(d["ticker"]))
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
        if watch:
            st.divider()
            st.caption("백업 (앱이 재시작되면 목록이 초기화될 수 있으니 "
                       "가끔 아래 내용을 복사해 두세요)")
            st.code(json.dumps(stt, ensure_ascii=False))
            with st.expander("백업 복원"):
                rb = st.text_area("복사해 둔 백업 붙여넣기", height=68)
                if st.button("복원", use_container_width=True):
                    try:
                        raw = json.loads(rb)
                        if isinstance(raw, list):
                            stt2 = {"tickers": raw, "bands": {}}
                        else:
                            stt2 = {"tickers": raw.get("tickers", []),
                                    "bands": raw.get("bands", {})}
                        save_state(stt2)
                        st.rerun()
                    except Exception:
                        st.error("붙여넣은 내용이 올바른 백업 형식이 아닙니다.")


if gate():
    main()
