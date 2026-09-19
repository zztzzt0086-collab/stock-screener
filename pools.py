"""
pools.py — 종목 풀 (구 finder.py에서 가져옴)
============================================
섹터별 내장 목록 + 위키피디아에서 S&P500 / 나스닥100 긁어오기.
cli.py find / scan 이 이걸 씀.
"""

import pandas as pd

# 상장폐지/인수/티커변경된 종목은 여기서 빼둔다.
#   CFLT (Confluent)  - 2026-09 기준 야후에서 조회 불가


SEMI = """
AMD NVDA MU INTC TXN ADI MCHP NXPI ON SWKS QRVO MPWR LSCC
ALAB CRDO ANET MRVL AVGO QCOM AMAT LRCX KLAC ASML TER ENTG
ACLS UCTT ICHR AEIS FORM COHU AMKR ONTO CAMT NVMI PLAB
SITM POWI SLAB DIOD VSH WOLF AOSL NVTS TSEM SMTC RMBS
SGH VECO AXTI IMOS AMBA CEVA QUIK GSIT PI SIMO
""".split()

DEFAULT = """
ALAB CRDO SITM POWI SLAB LSCC MPWR FORM ONTO CAMT NVMI ACLS
UCTT ICHR AEIS COHU PLAB AXTI AMBA CEVA PI SIMO RMBS VECO
NVTS AOSL DIOD SMTC GSIT QUIK IMOS TSEM SGH WOLF
""".split()


WIKI_CACHE = ".cache_wiki"       # 한 번 받은 지수 목록은 저장해 둔다
WIKI_TTL = 7 * 24 * 3600         # 일주일


def _wiki_cache_path(url):
    import hashlib
    import os
    os.makedirs(WIKI_CACHE, exist_ok=True)
    h = hashlib.md5(url.encode()).hexdigest()[:12]
    return os.path.join(WIKI_CACHE, h + ".json")


def pool_from_wiki(url, col_candidates):
    """위키피디아 표에서 티커 컬럼 추출.

    pandas.read_html 은 브라우저 정보를 보내지 않아 위키피디아가
    403(거부)으로 막는 경우가 있다. 직접 헤더를 붙여 받아온다.
    한 번 받은 목록은 일주일간 저장해 두고 재사용한다.
    """
    import json
    import os
    import time
    import urllib.request
    from io import StringIO

    p = _wiki_cache_path(url)
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < WIKI_TTL:
        try:
            with open(p, encoding="utf-8") as f:
                cached = json.load(f)
            if cached:
                print(f"  (저장된 목록 사용, {len(cached)}개)", end=" ")
                return cached
        except Exception:
            pass

    html = None
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"  내려받기 실패({e}).", end=" ")
        return []

    try:
        tables = pd.read_html(StringIO(html))
    except Exception as e:
        print(f"  표 해석 실패({e}).", end=" ")
        return []
    # 컬럼 이름을 너그럽게 찾는다.
    #   위키피디아가 "Ticker" → "Ticker symbol" 처럼 바꾸면
    #   정확히 일치로만 찾던 예전 코드는 표를 통째로 놓쳤다.
    #   (나스닥100 이 계속 실패하던 이유로 의심된다)
    want = {c.lower() for c in col_candidates}

    def _looks_like_ticker_col(name):
        n = str(name).strip().lower()
        if n in want:
            return True
        return any(w in n for w in ("ticker", "symbol"))

    seen_cols = []
    for t in tables:
        # 컬럼이 2단(MultiIndex)인 표도 있다. 납작하게 편다.
        cols = list(t.columns)
        flat = []
        for c in cols:
            if isinstance(c, tuple):
                flat.append(" ".join(str(x) for x in c if "Unnamed" not in str(x)))
            else:
                flat.append(str(c))
        seen_cols.append(flat)
        for c, name in zip(cols, flat):
            if not _looks_like_ticker_col(name):
                continue
            # ★ 위키피디아 표에는 눈에 안 보이는 문자가 섞여 있다.
            #   제로폭 공백(\u200b) · 소프트하이픈(\xad) · 줄바꿈 없는 공백(\xa0)
            #   화면엔 "NVDA" 로 보이지만 strip() 으로 안 떨어지고
            #   isalpha() 가 False 가 되어 통째로 걸러졌다.
            #   (나스닥100 이 계속 실패하던 진짜 이유)
            import re as _re
            def _clean(v):
                v = _re.sub(r"[\u200b\u200c\u200d\u00ad\ufeff\u00a0\s]", "", str(v))
                v = _re.sub(r"\[.*?\]", "", v)      # [1] 같은 각주 제거
                return v.strip()

            vals = [_clean(v) for v in t[c].dropna().astype(str)]
            # 점을 먼저 하이픈으로 바꾸고 검사한다.
            #   BRK.B · BF.B 처럼 점이 든 티커가 걸러지고 있었다.
            def _ok(v):
                w = v.replace(".", "-")
                return 1 <= len(w) <= 6 and w.replace("-", "").isalpha()

            out = [v.replace(".", "-") for v in vals if _ok(v)]
            if len(out) < 20 and vals:
                bad = [v for v in vals if not _ok(v)]
                print(f"  [{name}] 값 {len(vals)}개 중 티커 {len(out)}개."
                      f" 걸러진 예: {[repr(x) for x in bad[:3]]}", end="  ")
            if len(out) >= 20:          # 50 → 20. 작은 지수도 받기 위함
                try:
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump(out, f)
                except Exception:
                    pass
                return out

    # 실패했으면 무엇을 봤는지 알려 준다. 그래야 고칠 수 있다.
    print(f"  표 {len(tables)}개에서 티커 열을 못 찾음.", end=" ")
    for i, fl in enumerate(seen_cols[:4]):
        print(f"[{i}] {', '.join(fl[:6])}", end="  ")
    return []


ROBOT = """
ISRG NVDA TER ROK ABB HON EMR PTC ADSK ANSY CGNX IRBT SYM
OMCL AVAV KTOS RCAT UAVS BOTZ ARBE OUST INVZ LAZR LIDR MVIS
NNDM DM MKFG VLD SSYS PRLB XMTR FARO NDSN GRMN TRMB
""".split()

HEALTH = """
ISRG DXCM PODD TNDM IRTC NVCR PEN SWAV AXNX SILK VCYT NTRA
EXAS GH NVTA CDNA TXG PACB ILMN TMO DHR A WAT MTD BRKR
RGEN TECH CRL ICLR MEDP SYNH VEEV DOCS HQY PGNY
""".split()

ENERGY = """
FSLR ENPH SEDG RUN NOVA ARRY SHLS MAXN CSIQ JKS DQ
PLUG BE FCEL BLDP HYSR GEVO AMRC AGR NEE BEP CWEN
STEM FLNC EOSE ESS QS SLDP AMPX FREY
""".split()

SOFTWARE = """
CRWD PANW ZS S OKTA NET DDOG SNOW MDB ESTC GTLB
TEAM NOW WDAY VEEV HUBS ZI APPF BSY TYL MANH PCTY
PAYC PCOR ASAN MNDY SMAR DOCN FROG AI PLTR U
""".split()

SPACE = """
RKLB ASTS PL SPCE LUNR RDW BKSY SATS IRDM VSAT GSAT
AVAV KTOS LHX LMT NOC RTX BA HEI TDG CW MOG-A
""".split()

FINTECH = """
SQ PYPL AFRM UPST SOFI LC NU MELI STNE PAGS DLO
COIN HOOD IBKR MKTX TW VIRT MARA RIOT CLSK HUT
TOST LSPD SHOP GLBE WIX BIGC
""".split()

POOLS = {
    "semi": SEMI, "default": DEFAULT, "robot": ROBOT, "health": HEALTH,
    "energy": ENERGY, "software": SOFTWARE, "space": SPACE, "fintech": FINTECH,
}


# ═════════════════════════════════════════════════════════════
# 지수 편입 종목 자동 수집 (위키피디아)
#   S&P 1500 = 500(대형) + 400(중형) + 600(소형)
#   소형주까지 들어가야 "작고 빨리 크는 회사" 발굴이 의미가 있다.
# ═════════════════════════════════════════════════════════════

INDEX_SOURCES = {
    "sp500": ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
              {"Symbol", "Ticker"}, "S&P 500 (대형주)"),
    "sp400": ("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
              {"Symbol", "Ticker"}, "S&P 400 (중형주)"),
    "sp600": ("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
              {"Symbol", "Ticker"}, "S&P 600 (소형주)"),
    "nasdaq100": ("https://en.wikipedia.org/wiki/Nasdaq-100",
                  {"Ticker", "Symbol"}, "나스닥 100"),
}


def fetch_index(key):
    """지수 편입 종목 리스트. 실패하면 빈 리스트."""
    if key not in INDEX_SOURCES:
        return []
    url, cols, _ = INDEX_SOURCES[key]
    return pool_from_wiki(url, cols)


def get_pool(name):
    if name == "all":
        seen, out = set(), []
        for p in POOLS.values():
            for t in p:
                if t not in seen:
                    seen.add(t)
                    out.append(t)
        return out
    if name in POOLS:
        return POOLS[name]
    if name in INDEX_SOURCES:
        return fetch_index(name) or DEFAULT
    if name == "kosdaq":
        print("  코스닥은 티커에 .KQ 를 붙여 tickers.txt 로 넣어 쓰는 걸 권장.")
        return []
    return DEFAULT
