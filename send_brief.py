import os, json, time, yaml, feedparser, requests, datetime, pytz, re, difflib
from dotenv import load_dotenv

# =====================
# 튜닝 파라미터
# =====================
N_TOP = 10                 # 전송할 기사 개수
SUMMARY_LIMIT = 140        # 부분발췌 최대 길이(문자)
SLEEP_BETWEEN = 1.2        # 기사별 전송 간격(초)
WAR_BOOST = 400            # 전쟁/안보 기사 보너스 (최상단으로 끌어올림)
MAX_RETRIES = 3            # 항목별 최대 재시도
BACKOFF_SEC = 1.5          # 429/5xx 시 점진 대기(초)

# ---------- 환경 ----------
load_dotenv()
TOKEN_FILE = os.getenv("TOKEN_FILE", "token.json")
REST_KEY = os.getenv("KAKAO_REST_API_KEY")
CLIENT_SECRET = os.getenv("KAKAO_CLIENT_SECRET", "")
KST = pytz.timezone("Asia/Seoul")

TOKEN_URL = "https://kauth.kakao.com/oauth/token"
SEND_URL  = "https://kapi.kakao.com/v2/api/talk/memo/default/send"

# ---------- 중요 키워드(가중치만; 임계/필수 없음) ----------
# 제목에 포함되면 해당 가중치만 "더해" 점수 산출. (쓸데없는 단어는 가중치 0점)
KEYWORDS = {
    # 거시/금리/물가/성장
    "연준":10, "fed":10, "federal reserve":10, "fomc":10,
    "금리":9, "interest rate":9, "rate hike":9, "rate cut":9,
    "cpi":10, "inflation":10, "deflation":8, "pce":10,
    "gdp":8, "growth":7, "고용":7, "실업":7,
    "환율":9, "exchange rate":9, "달러":6, "dollar":6,
    "국채":7, "treasury":8, "bond":7, "bond yield":9, "국채금리":9,
    "양적긴축":8, "양적완화":8, "qe":7, "qt":7,
    "재정":7, "부양책":7, "stimulus":7, "감세":6, "tax cut":6,
    "관세":7, "tariff":7, "무역수지":8, "trade balance":8, "수출":7, "export":7,

    # 제재/공급망/산업정책
    "제재":10, "sanction":10, "export control":10,
    "칩스법":10, "chips act":10, "ira":10, "보조금":7, "subsidy":7,
    "공급망":9, "supply chain":9, "리쇼어링":8, "reshoring":8, "리슈어링":8, "우회수출":8,

    # 에너지/원자재
    "유가":9, "oil":9, "opec":9, "opec+":9,
    "천연가스":8, "natural gas":8, "gas":7,
    "wti":7, "brent":7, "브렌트":7, "구리":7, "copper":7,

    # 산업/기업 임팩트/규제
    "반도체":8, "semiconductor":8, "ai":5,
    "실적":8, "earnings":8, "guidance":7,
    "감자":9, "증자":9, "ipo":7,
    "상장폐지":10, "delisting":10, "파산":9, "bankruptcy":9, "default":9,
    "리콜":7, "recall":7,
    "독점금지":7, "antitrust":7, "규제":7, "regulation":7,
    "공정위":7, "ftc":7, "doj":7
}

# 전쟁/안보 키워드: 있으면 WAR_BOOST 부여
WAR_TERMS = {
    # 한글
    "전쟁","교전","공습","침공","미사일","핵","핵실험","핵개발","동원령","휴전",
    "분쟁","봉쇄","제해권","격추","무인기","드론 공격",
    "우크라","가자","이스라엘","하마스","헤즈볼라",
    "홍해","호르무즈","타이완","대만","남중국해","한반도","북한",
    # 영어/지명
    "war","battle","conflict","clash","missile","strike","airstrike","shelling","drone",
    "invasion","ceasefire","mobilization","blockade","shootdown",
    "ukraine","russia","gaza","israel","hamas","hezbollah",
    "red sea","hormuz","taiwan","south china sea","north korea","korean peninsula"
}

# 쓸데없는 주제(점수 0; 감점/제외 없음)
NO_SCORE_TERMS = {
    # 한글
    "연예","배우","아이돌","드라마","예능","스캔들","열애","결혼",
    "스포츠","야구","축구","농구","배구","골프","e스포츠",
    "날씨","폭염","무더위","비 예보","해수욕장","여행","관광","축제","문화",
    "인플루언서","유튜버","틱톡","소셜미디어","사생활",
    "공항 터미널","재개장","교통통제","사고 현장","생활","리빙","건강 팁","가십","지역 뉴스",
    # 영어
    "celebrity","gossip","entertainment","idol","k-pop","sports","soccer","baseball","tennis",
    "weather","heat wave","travel","festival","lifestyle","review","how-to","tips"
}
# NO_SCORE_TERMS는 단지 0점(가중치 없음)으로 취급. 제외/감점하지 않음.

# ---------- 점수 계산 ----------
def score_title(title: str) -> int:
    t = title.lower()
    # 전쟁/안보는 큰 가산점
    if any(w.lower() in t for w in WAR_TERMS):
        return WAR_BOOST
    s = 0
    # 중요 키워드 가중치 합산
    for k, w in KEYWORDS.items():
        if k.lower() in t:
            s += w
    # 지정학/거시 보너스 약간
    if any(x in t for x in [
        "연준","fed","fomc","opec","opec+","남중국해","south china sea",
        "타이완","taiwan","홍해","red sea","호르무즈","hormuz",
        "국채금리","bond yield","환율","exchange rate"
    ]):
        s += 2
    # 쓸데없는 단어는 점수 0(추가/감점 없음) — 아무 것도 하지 않음
    return s

# ---------- 토큰/전송 ----------
def load_token():
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_token(tok):
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(tok, f, ensure_ascii=False, indent=2)

def refresh_token(tok):
    data = {"grant_type": "refresh_token","client_id": REST_KEY,"refresh_token": tok["refresh_token"]}
    if CLIENT_SECRET:
        data["client_secret"] = CLIENT_SECRET
    r = requests.post(TOKEN_URL, data=data, timeout=15)
    r.raise_for_status()
    upd = r.json()
    tok["access_token"] = upd.get("access_token", tok["access_token"])
    if "refresh_token" in upd:
        tok["refresh_token"] = upd["refresh_token"]
    save_token(tok)
    return tok

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

# ---------- RSS/정리 ----------
def get_entries_from(feed_url, limit=12):
    try:
        d = feedparser.parse(feed_url)
        return d.entries[:limit]
    except Exception:
        return []

def clean_title(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def normalize(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\[[^\]]+\]", "", s)  # [단독][속보] 제거
    s = re.sub(r"\s+", " ", s)
    return s

def clean_summary(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)      # HTML 제거
    s = re.sub(r"&nbsp;?", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return (s[:SUMMARY_LIMIT-3] + "...") if len(s) > SUMMARY_LIMIT else s

def find_entry_info(sources: dict, section: str, title: str):
    """해당 섹션 RSS 안에서 제목 유사도가 가장 높은 엔트리의 summary/link를 가져온다."""
    best = ("", "")
    best_ratio = 0.0
    t0 = title.lower()
    for url in sources.get(section, []):
        d = feedparser.parse(url)
        for e in d.entries:
            et = getattr(e, "title", "") or ""
            ratio = difflib.SequenceMatcher(a=t0, b=et.lower()).ratio()
            if ratio > best_ratio:
                summary = getattr(e, "summary", getattr(e, "description", ""))
                link = getattr(e, "link", "")
                best = (clean_summary(summary), link)
                best_ratio = ratio
    return best  # (summary, link)

def format_item(item, idx=None):
    head = f"[{item['tag']}] {item['title']}"
    if idx is not None:
        head = f"{idx}. " + head
    lines = [head]
    if item.get("summary"):
        lines.append(f"↳ {item['summary']}")
    if item.get("link"):
        lines.append(f"링크: {item['link']}")
    return "\n".join(lines)[:950]

def send_item(access_token, item, idx=None):
    send_to_me(access_token, format_item(item, idx))

# ---------- 메인 빌드 ----------
def build_brief():
    now = datetime.datetime.now(KST)
    since = (now - datetime.timedelta(hours=14))
    date_str = now.strftime("%Y-%m-%d (%a)")

    with open("news_sources.yml", "r", encoding="utf-8") as f:
        sources = yaml.safe_load(f)

    # 섹션별 후보 수집
    section_candidates = {sec: [] for sec in sources.keys()}
    for section, feeds in sources.items():
        for url in feeds:
            for e in get_entries_from(url, limit=12):
                title = clean_title(getattr(e, "title", ""))
                if not title:
                    continue
                section_candidates[section].append(normalize(title))

    # 섹션 내부 중복 제거
    for sec in section_candidates:
        seen, uniq = set(), []
        for t in section_candidates[sec]:
            key = t.lower()
            if key in seen:
                continue
            seen.add(key); uniq.append(t)
        section_candidates[sec] = uniq

    # 전역 스코어링 → 점수순 상위 N_TOP
    ordered = ["korea","us","china","commodities","global"]
    name_map = {"korea":"한국","us":"미국","china":"중국","commodities":"원자재","global":"글로벌"}

    scored = []
    for sec in ordered:
        for t in section_candidates.get(sec, []):
            sc = score_title(t)
            scored.append((sec, t, sc))

    # 점수 내림차순 정렬 후 상위 10개
    scored.sort(key=lambda x: x[2], reverse=True)
    topN = scored[:N_TOP]

    # 기사별 summary/link 추출
    items = []
    for sec, title, _ in topN:
        summary, link = find_entry_info(sources, sec, title)
        items.append({
            "tag": name_map.get(sec, sec),
            "title": title,
            "summary": summary,
            "link": link
        })

    header = f"[아침 브리핑] {date_str}\n기간: {since.strftime('%m/%d %H:%M')}~{now.strftime('%m/%d %H:%M')} (KST)\n상위 {N_TOP}건을 순서대로 보냅니다."
    return header, items

# ---------- 실행 (안정적 전송 루프) ----------
def main():
    tok = load_token()
    header, items = build_brief()

    print(f"[DEBUG] selected items: {len(items)}")

    # 헤더 전송(401/429/5xx 대비)
    def safe_send_text(text):
        nonlocal tok
        try:
            send_to_me(tok["access_token"], text)
            return True
        except PermissionError:
            tok = refresh_token(tok)
            try:
                send_to_me(tok["access_token"], text)
                return True
            except Exception as e:
                print("[ERROR] header send failed after refresh:", e)
                return False
        except requests.exceptions.HTTPError as e:
            code = getattr(e.response, "status_code", None)
            if code in (429, 500, 502, 503, 504):
                time.sleep(BACKOFF_SEC)
                try:
                    send_to_me(tok["access_token"], text)
                    return True
                except Exception as e2:
                    print("[ERROR] header http error again:", e2)
                    return False
            else:
                print("[ERROR] header http error:", e)
                return False
        except Exception as e:
            print("[ERROR] header unknown error:", e)
            return False

    if not safe_send_text(header):
        print("[FATAL] cannot send header; aborting.")
        return

    # 본문 전송(안정 전송)
    for i, it in enumerate(items, 1):
        attempts = 0
        while attempts < MAX_RETRIES:
            try:
                send_item(tok["access_token"], it, idx=i)
                break  # 성공
            except PermissionError:
                tok = refresh_token(tok)
                attempts += 1
                if attempts >= MAX_RETRIES:
                    print(f"[ERROR] item {i}: 401 even after refresh; skipping.")
                    break
                continue
            except requests.exceptions.HTTPError as e:
                code = getattr(e.response, "status_code", None)
                if code in (429, 500, 502, 503, 504):
                    wait = BACKOFF_SEC * (attempts + 1)
                    print(f"[WARN] item {i}: HTTP {code}, retry in {wait:.1f}s...")
                    time.sleep(wait)
                    attempts += 1
                    continue
                else:
                    print(f"[ERROR] item {i}: HTTP error {code}; skipping.")
                    break
            except Exception as e:
                print(f"[ERROR] item {i}: unexpected error {e}; skipping.")
                break

        time.sleep(SLEEP_BETWEEN)  # 성공/실패와 무관하게 간격 유지

if __name__ == "__main__":
    main()
