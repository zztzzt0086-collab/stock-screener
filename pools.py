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

    # 실패했으면 표를 전부 보여 준다. 그래야 고칠 수 있다.
    print(f"\n  표 {len(tables)}개에서 티커 열을 못 찾음:")
    for i, (fl, t) in enumerate(zip(seen_cols, tables)):
        head = ""
        try:
            if len(t):
                head = " | 첫 줄: " + ", ".join(
                    str(x)[:14] for x in list(t.iloc[0])[:4])
        except Exception:
            pass
        print(f"    [{i:>2}] {len(t):>4}행  {', '.join(fl[:5])}{head}")
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
    # ★ 2026-09 확인: 위키피디아가 Nasdaq-100 문서에서 종목 표를 빼고
    #   별도 문서(List_of_NASDAQ-100_companies)로 옮겼다.
    #   옛 주소를 보던 코드는 지수 기록 표만 18개 읽고 실패했다.
    "nasdaq100": ("https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies",
                  {"Ticker", "Symbol"}, "나스닥 100"),
    # 위키피디아가 아니라 나스닥 공개 목록에서 받는다 (fetch_index 에서 갈라짐)
    "nasdaqall": (None, None, "나스닥 전체 상장사"),
    "nyseall": (None, None, "NYSE·AMEX 전체 상장사"),
}


def fetch_index(key):
    """지수 편입 종목 리스트. 실패하면 빈 리스트."""
    if key not in INDEX_SOURCES:
        return []
    if key == "nasdaqall":
        return fetch_all_listed("nasdaq")
    if key == "nyseall":
        return fetch_all_listed("nyse")
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

# ─────────────────────────────────────────────────────────────
# 나스닥 전체 상장사 (3,000개 이상)
#
#   나스닥이 공개하는 목록 파일을 그대로 받는다.
#   지수(나스닥100)는 그중 큰 것 100개뿐이라, 지수 밖 소형주까지
#   보려면 이 목록이 필요하다.
#
#   ★ 알고 쓸 것
#     대부분이 시총 1천억 미만이다. 업종 확인에만 1시간 반,
#     scan 은 4시간쯤 걸린다.
#     ETF·우선주·워런트는 걸러 낸다.
# ─────────────────────────────────────────────────────────────

NASDAQ_LIST_URL = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt")
NYSE_LIST_URL = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt")


def fetch_all_listed(which="nasdaq"):
    """나스닥(또는 NYSE 등) 전체 상장 종목.

    파일 형식: 파이프(|)로 나뉜 텍스트. 첫 줄이 머리글, 마지막 줄이 파일 안내.
    """
    import json
    import os
    import time
    import urllib.request
    url = NASDAQ_LIST_URL if which == "nasdaq" else NYSE_LIST_URL
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
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"  받기 실패: {e}")
        return []

    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 10:
        return []
    head = [h.strip() for h in lines[0].split("|")]
    out = []
    for l in lines[1:]:
        if l.startswith("File Creation Time"):
            break
        parts = [x.strip() for x in l.split("|")]
        if len(parts) != len(head):
            continue
        row = dict(zip(head, parts))
        sym = row.get("Symbol") or row.get("ACT Symbol") or ""
        # ETF·테스트종목 제외
        if row.get("ETF") == "Y" or row.get("Test Issue") == "Y":
            continue
        # 우선주·워런트·유닛은 티커에 특수문자가 붙는다
        if not sym or not sym.replace("-", "").isalpha():
            continue
        if len(sym) > 5:              # 5글자 초과는 우선주·워런트가 대부분
            continue
        out.append(sym)
    out = sorted(set(out))
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(out, f)
    except Exception:
        pass
    return out


# ─────────────────────────────────────────────────────────────
# 지수 편입 종목 — 티커 + 회사명 + 섹터 (앱 「지수 종목」 탭용)
#   pool_from_wiki 는 티커만 준다. 여기서는 표의 회사명·섹터 열도 같이 가져온다.
# ─────────────────────────────────────────────────────────────

INDEX_MEMBER_PAGES = {
    "sp500": ("S&P 500", "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"),
    "nasdaq100": ("나스닥 100", "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"),
    "kospi200": ("코스피 시총 상위 200", "yahoo:kospi_top200"),
}


def index_members(key):
    """지수 편입 종목 목록. [{"ticker","name","sector","sub"}, ...]. 실패하면 [].

    ★ 위키백과 표를 읽는다. 일주일 저장해 두고 재사용한다.
    ★ 티커의 점은 야후 형식에 맞춰 하이픈으로 바꾼다 (BRK.B → BRK-B).
    """
    import json, os, re, time, urllib.request
    from io import StringIO
    if key not in INDEX_MEMBER_PAGES:
        return []
    if key == "kospi200":
        # 네이버 코스피 200 페이지가 없어지고(410) 거래소는 로그인이 필요해져서,
        # 거래소 코스피 목록 + 야후 시총으로 '시총 상위 200' 을 만든다 (코스피 200 근사)
        top = kospi_top200()
        if not top:
            return []
        info = {x["code"]: x for x in kr_listing("kospi")}
        return [{"ticker": c + ".KS", "name": n,
                 "sector": info.get(c, {}).get("industry", ""),
                 "sub": info.get(c, {}).get("products", "")} for c, n, _m in top]
    _, url = INDEX_MEMBER_PAGES[key]
    p = _wiki_cache_path(url + "#members")
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < WIKI_TTL:
        try:
            with open(p, encoding="utf-8") as f:
                cached = json.load(f)
            if cached:
                return cached
        except Exception:
            pass
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", "replace")
        tables = pd.read_html(StringIO(html))
    except Exception:
        return []

    def _clean(v):
        v = re.sub(r"[\u200b\u200c\u200d\u00ad\ufeff\u00a0\s]", "", str(v))
        return re.sub(r"\[.*?\]", "", v).strip()

    def _col(cols, *cands):
        low = {str(c).strip().lower(): c for c in cols}
        for cd in cands:
            for lc, c in low.items():
                if cd in lc:
                    return c
        return None

    best = []
    for t in tables:
        cols = list(t.columns)
        c_t = _col(cols, "symbol", "ticker")
        c_n = _col(cols, "security", "company", "name")
        c_s = _col(cols, "gics sector", "sector", "icb industry", "industry")
        c_u = _col(cols, "gics sub-industry", "sub-industry", "subsector", "icb subsector")
        if c_t is None or c_n is None:
            continue
        rows = []
        for _, r in t.iterrows():
            tk = _clean(r[c_t]).replace(".", "-").upper()
            if not tk or len(tk) > 6 or not tk.replace("-", "").isalpha():
                continue
            rows.append({"ticker": tk,
                         "name": str(r[c_n]).strip(),
                         "sector": (str(r[c_s]).strip() if c_s is not None and str(r[c_s]) != "nan" else ""),
                         "sub": (str(r[c_u]).strip() if c_u is not None and str(r[c_u]) != "nan" else "")})
        seen, uniq = set(), []                  # 같은 티커 두 번 나오면 한 번만
        for r_ in rows:
            if r_["ticker"] not in seen:
                seen.add(r_["ticker"]); uniq.append(r_)
        if len(uniq) > len(best):
            best = uniq
    if len(best) < 20:
        return []
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(best, f, ensure_ascii=False)
    except Exception:
        pass
    return best


# ═════════════════════════════════════════════════════════════
# 한국 — 코스피 200 · 코스피/코스닥 전체 (업종 붙여서)
#   코스피 200 구성: 네이버 금융 편입 종목 페이지 (10개씩 20쪽)
#   업종·주요제품: 한국거래소 KIND 상장법인 목록 (로그인 없이 받는 공개 목록)
#   야후 티커: 코스피 .KS · 코스닥 .KQ
# ═════════════════════════════════════════════════════════════

KIND_URL = ("https://kind.krx.co.kr/corpgeneral/corpList.do?method=download"
            "&searchType=13&marketType={m}")
NAVER_K200 = "https://finance.naver.com/sise/entryJongmok.naver?type=KPI200&page={p}"

# 한국 업종 묶음 — KIND 업종 이름(한글)에 이 말이 들어가면 해당
KR_INDUSTRY_PRESETS = {
    "semi":  ["반도체", "전자부품", "인쇄회로", "전자집적회로"],
    "ai":    ["소프트웨어", "컴퓨터 프로그래밍", "시스템 통합", "자료처리", "호스팅",
              "포털", "정보서비스"],
    "power": ["전동기", "발전기", "전기 변환", "전기공급", "전기 공급", "절연선",
              "케이블", "축전지", "전지", "변압기", "전기장비"],
    "robot": ["로봇", "특수 목적용 기계", "일반 목적용 기계", "측정", "시험",
              "제어", "정밀기기"],
    "comm":  ["통신 및 방송 장비", "컴퓨터 및 주변장치", "영상 및 음향기기"],
    "bio":   ["의약품", "의약물질", "생물학적", "의료용", "의료기기"],
}
KR_INDUSTRY_PRESETS["ours"] = (KR_INDUSTRY_PRESETS["semi"] + KR_INDUSTRY_PRESETS["ai"]
                               + KR_INDUSTRY_PRESETS["power"] + KR_INDUSTRY_PRESETS["robot"]
                               + KR_INDUSTRY_PRESETS["comm"])
# 한국 목록을 못 받았을 때 이유 (화면에 보여 준다)
KR_LAST_ERR = {}

# 업종은 기계인데 주요제품이 반도체 장비인 회사(장비주)를 놓치지 않도록, 주요제품에서도 찾는 말
KR_PRODUCT_WORDS = ["반도체", "웨이퍼", "HBM"]


def _http_get(url, enc=None, timeout=30):
    """브라우저처럼 받아 온다. enc 를 주면 그 인코딩으로 푼다 (한국 사이트는 cp949)."""
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Referer": url.split("/", 3)[0] + "//" + url.split("/", 3)[2] + "/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    for e in ([enc] if enc else []) + ["utf-8", "cp949"]:
        try:
            return raw.decode(e)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def _kr_cache(key, loader, min_len, err_key=None):
    """일주일 저장. 너무 적게 받아졌으면(깨진 응답) 저장하지 않는다."""
    import json, os, time
    pth = _wiki_cache_path(key)
    if os.path.exists(pth) and time.time() - os.path.getmtime(pth) < WIKI_TTL:
        try:
            with open(pth, encoding="utf-8") as f:
                got = json.load(f)
            if len(got) >= min_len:
                return got
        except Exception:
            pass
    try:
        got = loader()
    except Exception as e:
        got = []
        if err_key and err_key not in KR_LAST_ERR:
            KR_LAST_ERR[err_key] = f"접속 오류 — {type(e).__name__}: {str(e)[:120]}"
    if len(got) < min_len and err_key and err_key not in KR_LAST_ERR:
        KR_LAST_ERR[err_key] = f"{len(got)}개만 받음 (최소 {min_len}개 필요)"
    if len(got) >= min_len:
        try:
            with open(pth, "w", encoding="utf-8") as f:
                json.dump(got, f, ensure_ascii=False)
        except Exception:
            pass
        return got
    return []


def _kr_code(v):
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s.zfill(6) if s.isdigit() else s.upper()


def kr_listing(market="kospi"):
    """코스피/코스닥 상장사 전체. [{"ticker","code","name","industry","products","market"}].
    실패하면 []. 스팩(기업인수목적회사)은 뺀다."""
    from io import StringIO
    m = {"kospi": "stockMkt", "kosdaq": "kosdaqMkt"}[market]
    suf = ".KS" if market == "kospi" else ".KQ"
    url = KIND_URL.format(m=m)

    def load():
        html = _http_get(url, "cp949")
        tables = pd.read_html(StringIO(html), header=0)
        if not tables:
            return []
        t = max(tables, key=len)
        cols = {str(c).strip(): c for c in t.columns}
        def col(*names):
            for n in names:
                for k, c in cols.items():
                    if n in k:
                        return c
            return None
        c_name, c_code = col("회사명", "종목명"), col("종목코드", "코드")
        c_ind, c_prod = col("업종"), col("주요제품")
        if c_name is None or c_code is None:
            return []
        out = []
        for _, r in t.iterrows():
            name = str(r[c_name]).strip()
            code = _kr_code(r[c_code])
            if not code or "스팩" in name or "기업인수목적" in name:
                continue
            ind = str(r[c_ind]).strip() if c_ind is not None and str(r[c_ind]) != "nan" else ""
            prod = str(r[c_prod]).strip() if c_prod is not None and str(r[c_prod]) != "nan" else ""
            out.append({"ticker": code + suf, "code": code, "name": name,
                        "industry": ind, "products": prod, "market": market})
        return out

    return _kr_cache(url, load, 100, err_key=market)


def kospi200_list():
    """코스피 200 구성 종목 [(code, name)]. 네이버 편입 종목 페이지를 넘기며 모은다."""
    import re

    def load():
        import os
        KR_LAST_ERR.pop("kospi200", None)
        seen, out, per = set(), [], []
        for pg in range(1, 30):
            try:
                html = _http_get(NAVER_K200.format(p=pg), "cp949")
            except Exception as e:
                KR_LAST_ERR["kospi200"] = (f"네이버 {pg}쪽 접속 오류 — {type(e).__name__}: {str(e)[:120]}"
                                           + (f" · 그 전까지 쪽별 {per}" if per else ""))
                break
            if pg == 1:
                try:           # 첫 쪽 원본을 남겨 둔다 (모양이 바뀌었는지 볼 때)
                    os.makedirs(WIKI_CACHE, exist_ok=True)
                    with open(os.path.join(WIKI_CACHE, "naver_k200_page1.html"), "w",
                              encoding="utf-8") as f:
                        f.write(html)
                except Exception:
                    pass
            # 링크 뒤에 다른 값(&, 따옴표 등)이 붙어도 잡는다
            got = re.findall(r'code=([0-9A-Z]{6})[^>]*>\s*([^<]+?)\s*</a>', html)
            new = [(c, n) for c, n in got if c not in seen]
            per.append(len(new))
            if not new:
                if pg == 1:
                    KR_LAST_ERR["kospi200"] = (f"네이버 첫 쪽에서 종목 링크를 못 찾음 (받은 글자 {len(html):,}자) "
                                               f"— 페이지 모양이 바뀐 듯. {WIKI_CACHE}\\naver_k200_page1.html 확인")
                elif len(out) < 150:
                    KR_LAST_ERR["kospi200"] = (f"{pg}쪽부터 새 종목이 안 나옴 · 쪽별 {per[:-1]} "
                                               f"→ 쪽 넘기기가 안 먹는 듯 ({len(out)}개만 받음)")
                break
            for c, n in new:
                seen.add(c)
                out.append([c, n])
        return out

    got = _kr_cache(NAVER_K200, load, 150, err_key="kospi200")
    return [tuple(x) for x in got]


def kr_match(item, key):
    """한국 종목이 업종 묶음(또는 쉼표로 적은 말)에 드는가."""
    key = (key or "").strip().lower()
    words = KR_INDUSTRY_PRESETS.get(key) or [w.strip() for w in key.split(",") if w.strip()]
    ind, prod = item.get("industry", ""), item.get("products", "")
    if any(w and w in ind for w in words):
        return True
    if key in ("semi", "ours") and any(w in prod for w in KR_PRODUCT_WORDS):
        return True
    return False


def kospi_top200(progress=None):
    """코스피 시총 상위 200 [(code, name, mcap)]. 코스피 200 을 로그인 없이 못 받게 되어 대신 쓴다.

    ★ 코스피 200 은 시총 큰 회사를 업종별로 고른 것이라 대부분 겹치지만 같지는 않다.
    1) 야후 종목 검색(yf.screen) — 한국·코스피·시총 큰 순 250개를 한 번에 (몇 초)
    2) 안 되면 거래소(KIND) 코스피 목록 전부에 야후 시총을 하나씩 붙인다 (몇 분)
    우선주는 거래소 목록에 없는 코드라 뺀다. 일주일 저장.
    """
    import yfinance as yf

    def load():
        KR_LAST_ERR.pop("kospi200", None)
        kind = {x["code"]: x for x in kr_listing("kospi")}
        rows = {}
        # 1) 한 번에
        try:
            q = yf.EquityQuery("and", [yf.EquityQuery("eq", ["region", "kr"]),
                                       yf.EquityQuery("is-in", ["exchange", "KSC"])])
            res = yf.screen(q, sortField="intradaymarketcap", sortAsc=False, size=250)
            for it in (res or {}).get("quotes", []):
                sym = str(it.get("symbol") or "")
                mc = it.get("marketCap")
                if not sym.endswith(".KS") or not mc:
                    continue
                code = sym[:-3]
                if kind and code not in kind:          # 우선주·리츠 등 거래소 목록 밖
                    continue
                nm = (kind.get(code) or {}).get("name") or it.get("shortName") or code
                rows[code] = (code, nm, float(mc))
        except Exception as e:
            KR_LAST_ERR["kospi200"] = f"야후 종목 검색 실패({type(e).__name__}) → 하나씩 받는 중"
        # 2) 하나씩 (1이 모자랄 때)
        if len(rows) < 150:
            if not kind:
                KR_LAST_ERR["kospi200"] = "거래소(KIND) 코스피 목록도 못 받아 시총을 붙일 대상이 없음"
                return []
            from concurrent.futures import ThreadPoolExecutor

            def one(code):
                try:
                    fi = yf.Ticker(code + ".KS").fast_info
                    mc = fi.get("marketCap") if hasattr(fi, "get") else getattr(fi, "market_cap", None)
                    return code, (float(mc) if mc else None)
                except Exception:
                    return code, None
            codes = list(kind)
            done = 0
            with ThreadPoolExecutor(max_workers=8) as ex:
                for code, mc in ex.map(one, codes):
                    done += 1
                    if progress and done % 20 == 0:
                        progress(done, len(codes))
                    if mc:
                        rows[code] = (code, kind[code]["name"], mc)
            if len(rows) < 150:
                KR_LAST_ERR["kospi200"] = f"야후 시총을 {len(rows)}개만 받음 (야후가 막았을 수 있음)"
        top = sorted(rows.values(), key=lambda x: -x[2])[:200]
        if len(top) >= 150:
            KR_LAST_ERR.pop("kospi200", None)       # 중간 메모는 성공하면 지운다
        return [list(x) for x in top]

    got = _kr_cache("kospi_top200_yahoo", load, 150, err_key="kospi200")
    return [tuple(x) for x in got]
