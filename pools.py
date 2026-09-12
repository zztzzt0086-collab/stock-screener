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
    for t in tables:
        for c in t.columns:
            if str(c).strip() in col_candidates:
                vals = t[c].dropna().astype(str).str.strip()
                out = [v.replace(".", "-") for v in vals
                       if 1 <= len(v) <= 6 and v.replace("-", "").isalpha()]
                if len(out) > 50:
                    try:
                        with open(p, "w", encoding="utf-8") as f:
                            json.dump(out, f)
                    except Exception:
                        pass
                    return out
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
