# send_brief_ci.py — CI 전용 (token.json 불필요)
import os, json, time, yaml, feedparser, requests, datetime, pytz, re, difflib, random
from dotenv import load_dotenv

# ===== 기본 설정 =====
N_TOP = 10
SUMMARY_LIMIT = 160
SLEEP_BETWEEN = 1.0
WAR_BOOST = 400
MAX_RETRIES = 3
BACKOFF_SEC = 1.5
MAX_KAKAO_TEXT = 950

KST = pytz.timezone("Asia/Seoul")
TOKEN_URL = "https://kauth.kakao.com/oauth/token"
SEND_URL  = "https://kapi.kakao.com/v2/api/talk/memo/default/send"

# .env 로컬 테스트용(액션에선 시크릿으로 주입)
load_dotenv()

# 번역 토글
AUTO_TRANSLATE = os.getenv("AUTO_TRANSLATE", "false").lower() == "true"
TRANSLATOR     = os.getenv("TRANSLATOR", "none").lower()   # none | libre
TARGET_LANG    = os.getenv("TARGET_LANG", "ko").lower()
TRANSLATE_URL  = os.getenv("TRANSLATE_URL", "").strip()
BILINGUAL      = os.getenv("BILINGUAL", "true").lower() == "true"

# 키워드/워드리스트
KEYWORDS = {
    "연준":10, "fed":10, "federal reserve":10, "fomc":10,
    "금리":9, "interest rate":9, "rate hike":9, "rate cut":9,
    "cpi":10, "inflation":10, "deflation":8, "pce":10,
    "gdp":8, "growth":7, "고용":7, "실업":7,
    "환율":9, "exchange rate":9, "달러":6, "dollar":6,
    "국채":7, "treasury":8, "bond":7, "bond yield":9, "국채금리":9,
    "양적긴축":8, "양적완화":8, "qe":7, "qt":7,
    "재정":7, "부양책":7, "stimulus":7, "감세":6, "tax cut":6,
    "관세":7, "tariff":7, "무역수지":8, "trade balance":8, "수출":7, "export":7,
    "제재":10, "sanction":10, "export control":10,
    "칩스법":10, "chips act":10, "ira":10, "보조금":7, "subsidy":7,
    "공급망":9, "supply chain":9, "리쇼어링":8, "reshoring":8, "리슈어링":8, "우회수출":8,
    "유가":9, "oil":9, "opec":9, "opec+":9,
    "천연가스":8, "natural gas":8, "gas":7,
    "wti":7, "brent":7, "브렌트":7, "구리":7, "copper":7,
    "반도체":8, "semiconductor":8, "ai":5,
    "실적":8, "earnings":8, "guidance":7,
    "감자":9, "증자":9, "ipo":7,
    "상장폐지":10, "delisting":10, "파산":9, "bankruptcy":9, "default":9,
    "리콜":7, "recall":7,
    "독점금지":7, "antitrust":7, "규제":7, "regulation":7,
    "공정위":7, "ftc":7, "doj":7
}
WAR_TERMS = {
    "전쟁","교전","공습","침공","미사일","핵","핵실험","핵개발","동원령","휴전",
    "분쟁","봉쇄","제해권","격추","무인기","드론 공격",
    "우크라","가자","이스라엘","하마스","헤즈볼라",
    "홍해","호르무즈","타이완","대만","남중국해","한반도","북한",
    "war","battle","conflict","clash","missile","strike","airstrike","shelling","drone",
    "invasion","ceasefire","mobilization","blockade","shootdown",
    "ukraine","russia","gaza","israel","hamas","hezbollah",
    "red sea","hormuz","taiwan","south china sea","north korea","korean peninsula"
}

def get_env(key, required=True):
    val = os.getenv(key)
    if val is None and required:
        raise RuntimeError(f"Missing env: {key}")
    return val

# ===== Kakao 토큰 발급: refresh_token -> access_token (token.json 미사용) =====
def get_access_token():
    data = {
        "grant_type": "refresh_token",
        "client_id": get_env("KAKAO_REST_API_KEY"),
        "refresh_token": get_env("KAKAO_REFRESH_TOKEN")
    }
    secret = os.getenv("KAKAO_CLIENT_SECRET", "")
    if secret:
        data["client_secret"] = secret
    r = requests.post(TOKEN_URL, data=data, timeout=15)
    r.raise_for_status()
    js = r.json()
    if js.get("refresh_token"):
        print("INFO: Kakao returned a new refresh_token. Update your repo secret KAKAO_REFRESH_TOKEN.")
    return js["access_token"]

def send_to_me(access_token: str, text: str):
    payload = {
        "template_object": json.dumps({
            "object_type": "text",
            "text": text,
            "link": {"web_url": "https://news.google.com/?hl=ko&gl=KR"},
            "button_title": "더 보기"
        }, ensure_ascii=False)
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.post(SEND_URL, headers=headers, data=payload, timeout=15)
    if r.status_code == 401:
        raise PermissionError("401 Unauthorized")
    r.raise_for_status()

# ===== RSS 유틸 =====
def get_entries_from(feed_url, limit=12):
    try:
        d = feedparser.parse(feed_url)
        return d.entries[:limit]
    except Exception:
        return []

def clean_title(s): return re.sub(r"\s+", " ", s or "").strip()
def normalize(s):
    s = (s or "").strip()
    s = re.sub(r"\[[^\]]+\]", "", s)
    return re.sub(r"\s+", " ", s)

def clean_summary(s):
    if not s: return ""
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"&nbsp;?", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return (s[:SUMMARY_LIMIT-3]+"...") if len(s)>SUMMARY_LIMIT else s

def score_title(title):
    t = title.lower()
    if any(w.lower() in t for w in WAR_TERMS): return WAR_BOOST
    s = 0
    for k, w in KEYWORDS.items():
        if k.lower() in t: s += w
    if any(x in t for x in ["연준","fed","fomc","opec","opec+","남중국해","south china sea",
                             "타이완","taiwan","홍해","red sea","호르무즈","hormuz",
                             "국채금리","bond yield","환율","exchange rate"]):
        s += 2
    return s

def smart_extractive_summary(title, summary):
    base = (summary or "")
    sents = re.split(r"(?<=[.!?])\s+", base)
    def sent_score(s):
        sc = 0
        if re.search(r"\d", s): sc += 2
        for k, w in KEYWORDS.items():
            if k.lower() in s.lower(): sc += min(3, w//4)
        return sc
    ranked = sorted(sents, key=sent_score, reverse=True)
    picked = []
    for s in ranked:
        if not s.strip(): continue
        if len(" ".join(picked)+" "+s) > SUMMARY_LIMIT: break
        picked.append(s.strip())
        if len(picked) >= 2: break
    text = " ".join(picked) if picked else base[:SUMMARY_LIMIT]
    return clean_summary(text)

# ===== 번역 유틸 =====
def _detect_lang():
    try:
        from langdetect import detect, DetectorFactory
        DetectorFactory.seed = 0
        return detect
    except Exception:
        return None
_DETECT = _detect_lang()

EN_KO_GLOSSARY = {
    "fomc": "연준회의(FOMC)", "fed": "연준(Fed)",
    "rate": "금리", "rates": "금리", "hike": "인상", "cut": "인하",
    "inflation": "인플레이션", "deflation": "디플레이션", "cpi": "CPI",
    "pce": "PCE", "gdp": "GDP", "yield": "수익률",
    "treasury": "미국 국채", "bond": "채권", "bonds": "채권",
    "oil": "유가", "brent": "브렌트유", "wti": "WTI",
    "dollar": "달러", "usd": "달러",
    "semiconductor": "반도체", "chips act": "칩스법", "ira": "IRA",
    "export control": "수출 통제", "sanction": "제재", "tariff": "관세",
}
def is_en(text):
    t = (text or "").strip()
    if not t: return False
    if _DETECT:
        try: return _DETECT(t) == "en"
        except Exception: pass
    letters = sum(c.isalpha() for c in t)
    ascii_letters = sum(('a' <= c.lower() <= 'z') for c in t)
    return letters>0 and ascii_letters/max(letters,1) > 0.85

def glos_translate_en2ko(text):
    if not text: return ""
    s = text
    for k,v in EN_KO_GLOSSARY.items():
        s = re.sub(rf"\b{k}\b", v, s, flags=re.IGNORECASE)
    s = re.sub(r"\bUSD\b", "달러", s, flags=re.IGNORECASE)
    return s

def libre_translate(text, target="ko"):
    if not TRANSLATE_URL: return ""
    try:
        r = requests.post(TRANSLATE_URL, json={"q": text, "source":"auto","target":target,"format":"text"}, timeout=8)
        if r.ok:
            return r.json().get("translatedText","")
    except Exception:
        pass
    return ""

def maybe_translate(title, summary):
    text = (summary or title or "").strip()
    if not AUTO_TRANSLATE or not is_en(text): return None
    tr = ""
    if TRANSLATOR == "libre":
        tr = libre_translate(text, target=TARGET_LANG)
    if not tr:
        tr = glos_translate_en2ko(text)
    return {"kr": tr, "original": text} if tr.strip() else None

# ===== 포맷 =====
def safe_cut(s, limit_bytes=950):
    b = s.encode("utf-8")
    return s if len(b)<=limit_bytes else b[:limit_bytes].decode("utf-8","ignore")

def format_item(item, idx=None):
    head = f"[{item['tag']}] {item['title']}"
    if idx is not None: head = f"{idx}. " + head
    lines = [head]
    if item.get("short"): lines.append(f"• 요약: {item['short']}")
    tr = item.get("translation")
    if tr:
        if BILINGUAL:
            lines.append(f"• 번역: {tr['kr']}")
            lines.append(f"• 원문: {tr['original']}")
        else:
            lines.append(f"• 번역: {tr['kr']}")
    if item.get("link"): lines.append(f"링크: {item['link']}")
    return safe_cut("\n".join(lines), 950)

def build_brief():
    now = datetime.datetime.now(KST)
    since = (now - datetime.timedelta(hours=14))
    date_str = now.strftime("%Y-%m-%d (%a)")
    with open("news_sources.yml","r",encoding="utf-8") as f:
        sources = yaml.safe_load(f)

    section_candidates = {sec: [] for sec in sources.keys()}
    for section, feeds in sources.items():
        for url in feeds:
            for e in get_entries_from(url, limit=12):
                title = clean_title(getattr(e,"title",""))
                if title: section_candidates[section].append(normalize(title))

    # dedup per section
    for sec in section_candidates:
        seen, uniq = set(), []
        for t in section_candidates[sec]:
            k = t.lower()
            if k in seen: continue
            seen.add(k); uniq.append(t)
        section_candidates[sec] = uniq

    ordered = ["korea","us","china","commodities","global"]
    name_map = {"korea":"한국","us":"미국","china":"중국","commodities":"원자재","global":"글로벌"}

    scored=[]
    for sec in ordered:
        for t in section_candidates.get(sec, []):
            scored.append((sec, t, score_title(t)))
    # 신규성 가벼운 흔들기(동률 타파용)
    random.shuffle(scored)
    scored.sort(key=lambda x:x[2], reverse=True)

    topN = scored[:N_TOP]

    items=[]
    for sec, title, _ in topN:
        best_summary, link, best_ratio = "", "", 0.0
        t0 = title.lower()
        for url in sources.get(sec, []):
            d = feedparser.parse(url)
            for e in d.entries:
                et = getattr(e,"title","") or ""
                r = difflib.SequenceMatcher(a=t0, b=et.lower()).ratio()
                if r > best_ratio:
                    summary = getattr(e,"summary", getattr(e,"description",""))
                    best_summary = clean_summary(summary)
                    link = getattr(e,"link","")
                    best_ratio = r
        # 유사도 임계 미달 시 요약 비움
        if best_ratio < 0.60:
            best_summary = ""
        short = smart_extractive_summary(title, best_summary)
        translation = maybe_translate(title, best_summary)
        items.append({
            "tag": name_map.get(sec, sec),
            "title": title,
            "short": short,
            "translation": translation,
            "link": link
        })

    header = f"[아침 브리핑] {date_str}\n기간: {since.strftime('%m/%d %H:%M')}~{now.strftime('%m/%d %H:%M')} (KST)\n상위 {N_TOP}건을 순서대로 보냅니다."
    return header, items

def main():
    access = get_access_token()
    header, items = build_brief()

    def safe_send(text):
        nonlocal access
        try:
            send_to_me(access, text); return True
        except PermissionError:
            access = get_access_token()
            send_to_me(access, text); return True

    safe_send(header)
    for i, it in enumerate(items, 1):
        attempts=0
        while attempts<MAX_RETRIES:
            try:
                send_to_me(access, format_item(it, idx=i)); break
            except PermissionError:
                access = get_access_token(); attempts+=1; continue
            except requests.exceptions.HTTPError as e:
                code = getattr(e.response, "status_code", None)
                if code in (429,500,502,503,504):
                    wait = BACKOFF_SEC*(attempts+1) + random.uniform(0,0.7)
                    time.sleep(wait); attempts+=1; continue
                else:
                    print(f"[ERROR] item {i}: HTTP {code}; skip."); break
            except Exception as e:
                print(f"[ERROR] item {i}: {e}"); break
        time.sleep(SLEEP_BETWEEN)

if __name__ == "__main__":
    main()
