"""
Translation server with operator dashboard.
"""
import os
import sys
import io
import json
import queue
import re
import threading
import anthropic
from flask import Flask, request, jsonify, Response, send_from_directory
from flask_sock import Sock
from dotenv import load_dotenv

# 실행 위치 판별: .exe로 묶였을 때와 일반 실행을 모두 지원
#  - BUNDLE_DIR: 화면 파일 등 '내장' 자원 위치 (읽기 전용)
#  - APP_DIR   : 사용자 파일 위치 (.env, google-key.json, 로그 등)
#                .exe 실행 시 → .exe 폴더 (쓰기 불가하면 %APPDATA%\LiveWord)
#                개발 실행 시 → 스크립트 폴더
if getattr(sys, "frozen", False):
    BUNDLE_DIR = sys._MEIPASS
    # 1순위: .exe 폴더 (설치 위치가 사용자 폴더라 대부분 쓰기 가능,
    #         AppData는 Windows 앱 격리로 접근이 차단되는 경우가 있음)
    # 2순위: %APPDATA%\LiveWord
    _exe_dir = os.path.dirname(sys.executable)
    try:
        _t = os.path.join(_exe_dir, ".write_test")
        with open(_t, "w") as _f:
            _f.write("ok")
        os.remove(_t)
        APP_DIR = _exe_dir
    except Exception:
        APP_DIR = os.path.join(os.environ.get("APPDATA", _exe_dir),
                               "LiveWord")
        os.makedirs(APP_DIR, exist_ok=True)
else:
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = BUNDLE_DIR

# 설정(.env)은 APP_DIR에서 읽음
load_dotenv(os.path.join(APP_DIR, ".env"), override=True, encoding="utf-8-sig")

# Google Cloud 인증: .exe 옆 google-key.json 자동 사용
_key_path = os.path.join(APP_DIR, "google-key.json")
if os.path.exists(_key_path) and "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ:
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _key_path

# 이 프로그램의 버전 — 새 버전 알림 비교 기준 (배포 시 함께 올림)
APP_VERSION = "1.1"
UPDATE_API = "https://api.github.com/repos/jjosh-oh/Translate-godword/releases/latest"

app = Flask(__name__)
sock = Sock(app)


@app.after_request
def _no_cache_html(resp):
    """HTML 페이지는 항상 최신본을 받도록 캐시 금지.
    (업데이트 후 폰에 옛 화면이 남아 자막/언어가 잘못 보이는 문제 방지)
    WebSocket/SSE/JSON 응답은 건드리지 않는다."""
    ctype = resp.headers.get("Content-Type", "")
    if ctype.startswith("text/html"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


# ── ngrok 자동 시작 ─────────────────────────────────────────────────────────
_ngrok_url = ""  # 실제 연결된 공개 URL (터널이 뜨면 채워짐)

def _start_ngrok():
    """백그라운드에서 ngrok 터널을 시작하고 URL을 _ngrok_url에 저장."""
    global _ngrok_url
    import subprocess, time, re

    authtoken = os.environ.get("NGROK_AUTHTOKEN", "").strip()
    domain = os.environ.get("NGROK_DOMAIN", "").strip()
    if not authtoken:
        return  # 설정 안 됐으면 건너뜀

    # ngrok 실행 파일: .exe 폴더에 번들된 것 우선, 없으면 시스템 PATH
    ngrok_exe = "ngrok"
    if getattr(sys, "frozen", False):
        _bundled = os.path.join(os.path.dirname(sys.executable), "ngrok.exe")
        if os.path.exists(_bundled):
            ngrok_exe = _bundled

    # 고정 도메인이 있으면 URL을 미리 설정 (파싱 기다릴 필요 없음)
    if domain:
        _ngrok_url = "https://" + domain
        print(f"[ngrok] 고정 도메인: {_ngrok_url}")

    # authtoken 등록 (최초 1회 또는 변경 시)
    try:
        subprocess.run([ngrok_exe, "config", "add-authtoken", authtoken],
                       capture_output=True, timeout=10)
    except Exception:
        return

    # 터널 시작
    cmd = [ngrok_exe, "http", "5000", "--log=stdout", "--log-format=json"]
    if domain:
        cmd += ["--domain", domain]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        print("[ngrok] ngrok을 찾을 수 없습니다. 설치 여부를 확인하세요.")
        return

    # stdout에서 URL 파싱 (고정 도메인 없는 경우 여기서 URL 확보)
    if not domain:
        for line in proc.stdout:
            try:
                obj = json.loads(line)
                url = obj.get("url") or obj.get("public_url") or ""
                if url.startswith("https://"):
                    _ngrok_url = url
                    print(f"[ngrok] 터널 연결: {_ngrok_url}")
                    break
            except Exception:
                if "url=" in line:
                    m = re.search(r"url=(https://\S+)", line)
                    if m:
                        _ngrok_url = m.group(1)
                        print(f"[ngrok] 터널 연결: {_ngrok_url}")
                        break

if os.environ.get("NGROK_AUTHTOKEN"):
    threading.Thread(target=_start_ngrok, daemon=True).start()

# Anthropic 클라이언트: 키가 없어도 서버는 떠야 하므로 지연 생성
client = None
def get_client():
    global client
    if client is None:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    return client

# Gemini 클라이언트(비교용) — Vertex AI 경유, 실패해도 서버는 정상 동작
GEMINI_PROJECT = "project-71ecd8e1-9699-417f-ae4"
gemini_client = None
try:
    from google import genai as _genai
    gemini_client = _genai.Client(vertexai=True, project=GEMINI_PROJECT, location="us-central1")
except Exception as _e:
    print("Gemini 초기화 건너뜀:", _e)

# 음성인식 언어 코드 매핑 (Google Speech 형식)
STT_LANG = {
    "ko-KR": "ko-KR", "en-US": "en-US", "ja-JP": "ja-JP",
    "zh-CN": "cmn-Hans-CN", "es-ES": "es-ES", "fr-FR": "fr-FR", "de-DE": "de-DE",
}
STT_TO_NAME = {
    "ko-KR": "Korean", "en-US": "English", "ja-JP": "Japanese",
    "zh-CN": "Chinese", "es-ES": "Spanish", "fr-FR": "French", "de-DE": "German",
}

# 여러 기기(송출창 + 셀폰들)에 동시 전송하기 위한 구독자 기반 브로드캐스트
_display_subscribers = set()
_operator_subscribers = set()
_sub_lock = threading.Lock()


class _Broadcaster:
    """put() 호출 시 모든 구독자 큐에 메시지를 복사해 넣는다."""
    def __init__(self, subscribers):
        self._subs = subscribers

    def put(self, item):
        with _sub_lock:
            for q in list(self._subs):
                q.put(item)

    def subscribe(self):
        q = queue.Queue()
        with _sub_lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q):
        with _sub_lock:
            self._subs.discard(q)


operator_queue = _Broadcaster(_operator_subscribers)


class LangHub:
    """언어별 자막 라우팅 허브.
    - subscribe(lang): 특정 언어 구독(셀폰) — 그 언어로 번역될 때만 자막 수신
    - subscribe_primary(): 대표 언어 구독(송출창) — 운영자가 고른 현재 대표 언어 수신
    실제 구독자가 있는 언어 + 대표 언어만 번역하므로, 아무도 안 고른 언어는 비용 0."""
    def __init__(self):
        self._lang_subs = {}       # lang -> set(Queue)  (셀폰: 언어별)
        self._primary_subs = set() # 송출창: 현재 대표 언어를 따라감
        self._lock = threading.Lock()

    def subscribe(self, lang):
        q = queue.Queue()
        with self._lock:
            self._lang_subs.setdefault(lang, set()).add(q)
        return q

    def subscribe_primary(self):
        q = queue.Queue()
        with self._lock:
            self._primary_subs.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._primary_subs.discard(q)
            for subs in self._lang_subs.values():
                subs.discard(q)

    def publish(self, lang, item, is_primary=False):
        with self._lock:
            targets = list(self._lang_subs.get(lang, ()))
            if is_primary:
                targets += list(self._primary_subs)
        for q in targets:
            q.put(item)

    def broadcast(self, item):
        """설정 등 모든 구독자에게 공통 전송."""
        with self._lock:
            targets = list(self._primary_subs)
            for subs in self._lang_subs.values():
                targets += list(subs)
        for q in targets:
            q.put(item)

    def active_languages(self):
        with self._lock:
            return {lang for lang, subs in self._lang_subs.items() if subs}

    def phone_stats(self):
        """셀폰(언어 지정 접속) 수 집계. 송출창(대표 구독)은 제외."""
        with self._lock:
            by_lang = {lang: len(subs) for lang, subs in self._lang_subs.items() if subs}
        return {"total": sum(by_lang.values()), "by_lang": by_lang}


hub = LangHub()

# Shared display settings
settings = {
    "font_size": 5,
    "text_color": "#ffffff",
    "bg_color": "#000000",
    "delay": 0,
    "target_lang": "English",
    "source_lang": "Korean",
    "voice": False,   # 번역 음성(TTS) 사용 여부 (운영자 토글, 기본 꺼짐)
}

# 번역 언어 이름 → Google TTS 언어 코드
TTS_LANG = {
    "English": "en-US", "Korean": "ko-KR", "Japanese": "ja-JP",
    "Chinese": "cmn-CN", "Spanish": "es-ES", "French": "fr-FR",
    "German": "de-DE", "Arabic": "ar-XA",
}

last_input = ""
last_output = ""
sermon_context = ""  # 업로드된 설교 자료

# 교회 용어집(인식 힌트 전용) — glossary.txt에서 한 줄에 하나씩 로드
glossary_terms = []


def load_glossary():
    global glossary_terms
    # .exe 옆 사용자 용어집을 우선, 없으면 내장 기본 용어집
    path = os.path.join(APP_DIR, "glossary.txt")
    if not os.path.exists(path):
        path = os.path.join(BUNDLE_DIR, "glossary.txt")
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [ln.strip() for ln in f.read().splitlines()]
        glossary_terms = [ln for ln in lines if ln]
    except Exception:
        glossary_terms = []


load_glossary()

# 번역 대응표 (한글=English 형식)
translation_mapping = {}

def load_mapping():
    global translation_mapping
    path = os.path.join(APP_DIR, "mapping.txt")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and line:
                        k, v = line.split("=", 1)
                        translation_mapping[k.strip()] = v.strip()
    except Exception:
        translation_mapping = {}

load_mapping()


# ===== 번역 비용 추적 =====
# claude-opus-4-8 요금(100만 토큰당 달러). 캐시 기록은 1.25배, 캐시 읽기는 0.1배.
PRICE_IN, PRICE_OUT = 5.00, 25.00
PRICE_CACHE_WRITE, PRICE_CACHE_READ = PRICE_IN * 1.25, PRICE_IN * 0.10

_cost_total = 0.0          # 프로그램 켠 뒤 누적(대표 언어 + 폰이 고른 언어 전부)
_cache_miss_streak = 0     # 캐시가 연속으로 빗나간 횟수
_cost_lock = threading.Lock()


def _usage_cost(u):
    return (getattr(u, "input_tokens", 0) * PRICE_IN
            + getattr(u, "cache_creation_input_tokens", 0) * PRICE_CACHE_WRITE
            + getattr(u, "cache_read_input_tokens", 0) * PRICE_CACHE_READ
            + getattr(u, "output_tokens", 0) * PRICE_OUT) / 1e6


def _track_cost(u):
    """이번 호출 비용을 누적하고, 캐시가 계속 빗나가면 경고 문구를 돌려준다.
    설교 원고처럼 큰 프롬프트는 캐시가 적중해야 값이 1/12로 떨어진다.
    프롬프트 앞쪽이 매 문장 바뀌면 캐시가 통째로 무효가 되어 요금이 폭증한다."""
    global _cost_total, _cache_miss_streak
    if u is None:
        return None, 0.0
    cost = _usage_cost(u)
    warn = None
    with _cost_lock:
        _cost_total += cost
        if (getattr(u, "cache_creation_input_tokens", 0) >= 1000
                and getattr(u, "cache_read_input_tokens", 0) == 0):
            _cache_miss_streak += 1
            if _cache_miss_streak in (10, 50, 200) or _cache_miss_streak % 500 == 0:
                warn = ("[경고] 캐시 미적중 %d회 연속 — 큰 프롬프트가 매번 새로 청구되고 "
                        "있습니다. 요금이 10배 이상 늘어납니다." % _cache_miss_streak)
        else:
            _cache_miss_streak = 0
        total = _cost_total
    return warn, total


def _log_translation(src, out, usage=None, warn=None, total=0.0):
    try:
        path = os.path.join(APP_DIR, "로그.txt")
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] 입력: {src}\n[{ts}] 번역: {out}\n")
            if usage is not None:
                f.write("[%s] 토큰: 입력 %d · 캐시읽기 %d · 캐시기록 %d · 출력 %d"
                        " | 이번 $%.4f · 누적 $%.2f\n"
                        % (ts, getattr(usage, "input_tokens", 0),
                           getattr(usage, "cache_read_input_tokens", 0),
                           getattr(usage, "cache_creation_input_tokens", 0),
                           getattr(usage, "output_tokens", 0),
                           _usage_cost(usage), total))
            if warn:
                f.write(f"[{ts}] {warn}\n")
            f.write("\n")
    except Exception:
        pass


def translate_and_stream(text: str, target_lang: str, source_lang: str, is_primary: bool = True, context: str = ""):
    global last_output
    base_instruction = (
        f"You are a professional church interpreter providing live subtitles. "
        f"Translate ONLY the exact given text from {source_lang} into {target_lang}. "
        f"Output ONLY the translation of what is given — never add, continue, complete, "
        f"or quote additional text. If the input is a short or partial sentence (e.g. part of "
        f"a Bible verse), translate only that fragment; do NOT finish the verse or add the rest. "
        f"No explanations or commentary."
    )

    # 번역 대응표가 있으면 프롬프트에 추가
    mapping_text = ""
    if translation_mapping:
        pairs = "\n".join(f"  {k} → {v}" for k, v in translation_mapping.items())
        mapping_text = (
            "\n\nName/term translation table (use these EXACT translations when the term appears):\n"
            + pairs
        )

    # 바로 앞 문장들(문맥) — 번역하지 말고 흐름/대명사/용어를 자연스럽게 잇는 용도
    context_block = ""
    if context:
        context_block = (
            f"\n\nPreceding {source_lang} text (context ONLY — do NOT translate, repeat, or output it; "
            f"use it only so pronouns, referents, terms, and sentence flow stay natural):\n{context}"
        )

    if sermon_context:
        # 설교 원고: 고유명사 확인 '참고용'으로만 — 내용을 이어 쓰지 않도록 명시
        reference = (
            "The sermon script below is a REFERENCE ONLY, used solely to spell proper nouns "
            "(biblical names, place names, Korean locations) consistently. "
            "Do NOT translate, output, continue, or copy any sentence from this script. "
            "Only translate the user's given text.\n\n"
            "--- SERMON REFERENCE (do not output) ---\n" + sermon_context[:8000] + "\n--- END ---"
        )
        # 캐시는 '앞에서부터' 같아야 적중한다. 문맥(context_block)은 문장마다
        # 달라지므로 반드시 캐시 블록 뒤에 둔다. 앞에 두면 설교 원고 5천여
        # 토큰이 매 문장 새로 청구되어 요금이 6배 이상 뛴다. (실제로 겪음)
        system = [
            {"type": "text", "text": base_instruction + mapping_text},
            {"type": "text", "text": reference,
             "cache_control": {"type": "ephemeral"}},
        ]
        if context_block:
            system.append({"type": "text", "text": context_block})
    else:
        system = base_instruction + mapping_text + context_block

    hub.publish(target_lang, "__clear__", is_primary)
    if is_primary:
        operator_queue.put(("output_clear", ""))

    # 출력 폭주 방지: 입력 길이에 비례한 상한(최소 256, 최대 1024 토큰)
    max_out = max(256, min(1024, len(text) * 4))

    result = []
    usage = None
    with get_client().messages.stream(
        model="claude-opus-4-8",
        max_tokens=max_out,
        system=system,
        messages=[{"role": "user", "content": text}],
    ) as stream:
        for chunk in stream.text_stream:
            result.append(chunk)
            hub.publish(target_lang, chunk, is_primary)
            if is_primary:
                operator_queue.put(("output_chunk", chunk))
        try:
            usage = stream.get_final_message().usage
        except Exception:
            usage = None

    out = "".join(result)
    hub.publish(target_lang, "__done__", is_primary)
    # 비용은 모든 언어를 합산하고, 로그 줄은 대표 언어에만 남긴다
    warn, total = _track_cost(usage)
    if warn:
        print(warn)
    if is_primary:
        last_output = out
        _log_translation(text, out, usage, warn, total)
        operator_queue.put(("done", ""))

    # 번역 음성(TTS): 운영자가 켰을 때만, 각 언어 셀폰(이어폰)으로 방송.
    # (2단계: 대표 언어 + 셀폰이 고른 보조 언어 모두 — 각자 자기 언어로 들음.
    #  구독자가 없는 언어는 번역 자체가 안 돌므로 음성 비용도 발생하지 않음.)
    if settings.get("voice") and out.strip():
        tts_jobs.put((out, target_lang))


# ===== 번역 음성(Google Cloud TTS) =====
_tts_client = None


def _get_tts_client():
    global _tts_client
    if _tts_client is None:
        from google.cloud import texttospeech
        _tts_client = texttospeech.TextToSpeechClient()
    return _tts_client


def _tts_and_broadcast(text, target_lang):
    """번역문을 Google TTS로 합성해 해당 언어 셀폰에 오디오로 방송."""
    import base64
    try:
        from google.cloud import texttospeech
        code = TTS_LANG.get(target_lang, "en-US")
        resp = _get_tts_client().synthesize_speech(
            input=texttospeech.SynthesisInput(text=text),
            voice=texttospeech.VoiceSelectionParams(language_code=code),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3),
        )
        b64 = base64.b64encode(resp.audio_content).decode()
        # is_primary=False → 송출창(대표 구독)엔 안 가고 해당 언어 셀폰에만 전달
        hub.publish(target_lang, ("__audio__", b64), is_primary=False)
    except Exception as e:
        print("TTS 오류:", str(e)[:200])


# ===== 번역 작업 큐 (문장 단위 번역을 순서대로 처리) =====
# 대표 언어(송출창)와 보조 언어(셀폰의 다른 언어)를 별도 워커로 처리해,
# 보조 언어 번역이 대표 화면 자막 속도를 늦추지 않도록 한다.
primary_jobs = queue.Queue()
secondary_jobs = queue.Queue()

# 음성 세션 관리: 마이크를 끄면 세션이 바뀌어 대기 중 번역이 폐기됨
_current_session = 0
_session_lock = threading.Lock()


def _run_job(job, is_primary):
    text, target_lang, source_lang, session, context = job
    # 세션이 유효한 경우에만 번역 (None이면 수동 입력 → 항상 실행)
    if session is not None:
        with _session_lock:
            if session != _current_session:
                return  # 마이크가 꺼진 뒤의 오래된 작업 → 폐기
    try:
        translate_and_stream(text, target_lang, source_lang, is_primary, context)
    except Exception as e:
        print("번역 오류:", e)


def _primary_worker():
    while True:
        _run_job(primary_jobs.get(), True)


def _secondary_worker():
    while True:
        _run_job(secondary_jobs.get(), False)


threading.Thread(target=_primary_worker, daemon=True).start()
threading.Thread(target=_secondary_worker, daemon=True).start()


# ── 번역 음성(TTS) 작업 큐 ──
# 문장을 순서대로(직렬) 합성해야 소리 순서가 자막과 일치한다.
# 동시 스레드로 합성하면 문장별 합성 시간 차로 소리 순서가 뒤바뀐다.
tts_jobs = queue.Queue()


def _tts_worker():
    while True:
        text, target_lang = tts_jobs.get()
        _tts_and_broadcast(text, target_lang)


threading.Thread(target=_tts_worker, daemon=True).start()


# 최근 원문 문장(문맥용) — 조각 번역이 앞뒤 흐름을 참고하도록 함
from collections import deque as _deque
_recent_src = _deque(maxlen=3)
_recent_lock = threading.Lock()


def reset_context():
    with _recent_lock:
        _recent_src.clear()


def enqueue_translation(text, source_lang, session=None):
    """대표 언어 + 현재 셀폰이 구독한 언어들로 번역을 예약.
    아무도 안 고른 언어는 큐에 들어가지 않으므로 비용이 발생하지 않는다."""
    primary = settings["target_lang"]
    # 이번 문장 '이전'까지의 문맥을 계산한 뒤, 현재 문장을 기록
    with _recent_lock:
        context = " ".join(_recent_src)
        _recent_src.append(text)
    primary_jobs.put((text, primary, source_lang, session, context))
    for lang in hub.active_languages():
        if lang != primary:
            secondary_jobs.put((text, lang, source_lang, session, context))


@app.route("/")
def index():
    return send_from_directory(BUNDLE_DIR, "display.html")


@app.route("/operator")
def operator():
    return send_from_directory(BUNDLE_DIR, "operator.html")


@app.route("/mobile")
def mobile():
    return send_from_directory(BUNDLE_DIR, "mobile.html")


@app.route("/guide")
def guide():
    return send_from_directory(BUNDLE_DIR, "guide.html")


@app.route("/guide-en")
def guide_en():
    return send_from_directory(BUNDLE_DIR, "guide-en.html")


@app.route("/poster")
def poster():
    return send_from_directory(BUNDLE_DIR, "poster.html")


@app.route("/compare")
def compare():
    return send_from_directory(BUNDLE_DIR, "compare.html")


def _translate_claude(text, target_lang, source_lang):
    system = (f"You are a professional church interpreter. Translate from {source_lang} into {target_lang}. "
              f"Output ONLY the translation.")
    m = get_client().messages.create(model="claude-opus-4-8", max_tokens=1024,
                               system=system, messages=[{"role": "user", "content": text}])
    return "".join(b.text for b in m.content if getattr(b, "type", "") == "text")


def _translate_gemini(text, target_lang, source_lang):
    if not gemini_client:
        return "(Gemini 사용 불가)"
    prompt = (f"You are a professional church interpreter. Translate from {source_lang} into {target_lang}. "
              f"Output ONLY the translation.\n\n{text}")
    r = gemini_client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    return (r.text or "").strip()


@sock.route("/audio-compare")
def audio_compare(ws):
    """비교용: 음성인식 후 같은 문장을 Claude·Gemini로 번역해 나란히 전송."""
    from google.cloud import speech
    import time as _t

    cfg = json.loads(ws.receive())
    src_code = STT_LANG.get(cfg.get("source_lang_code"), "ko-KR")
    source_name = STT_TO_NAME.get(cfg.get("source_lang_code"), "Korean")
    target_lang = cfg.get("target_lang", "English")

    speech_client = speech.SpeechClient()
    recog_config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=16000, language_code=src_code,
        enable_automatic_punctuation=True, model="latest_long", use_enhanced=True,
    )
    streaming_config = speech.StreamingRecognitionConfig(config=recog_config, interim_results=True)

    audio_q = queue.Queue()
    stop_flag = {"stop": False}
    send_lock = threading.Lock()

    def safe_send(obj):
        try:
            with send_lock:
                ws.send(json.dumps(obj))
        except Exception:
            pass

    def receiver():
        try:
            while True:
                data = ws.receive()
                if data is None:
                    break
                if isinstance(data, (bytes, bytearray)):
                    audio_q.put(bytes(data))
        except Exception:
            pass
        finally:
            stop_flag["stop"] = True
            audio_q.put(None)

    threading.Thread(target=receiver, daemon=True).start()

    def request_gen():
        while not stop_flag["stop"]:
            chunk = audio_q.get()
            if chunk is None:
                return
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

    def run_engine(engine, fn, text):
        t0 = _t.time()
        try:
            out = fn(text, target_lang, source_name)
        except Exception as e:
            out = "(오류: " + str(e)[:60] + ")"
        ms = int((_t.time() - t0) * 1000)
        safe_send({"engine": engine, "text": out, "ms": ms})

    import re as _rec

    def split_sentences(t):
        return [p for p in _rec.split(r'(?<=[\.\?\!。？！…])\s*', t) if p.strip()]

    def translate_both(text):
        safe_send({"engine": "input", "text": text})
        threading.Thread(target=run_engine, args=("claude", _translate_claude, text), daemon=True).start()
        threading.Thread(target=run_engine, args=("gemini", _translate_gemini, text), daemon=True).start()

    translated = []

    while not stop_flag["stop"]:
        try:
            responses = speech_client.streaming_recognize(streaming_config, request_gen())
            for response in responses:
                for result in response.results:
                    tr = result.alternatives[0].transcript
                    if not tr.strip():
                        continue
                    pieces = split_sentences(tr)
                    if result.is_final:
                        for p in pieces:
                            n = p.strip()
                            if n and n not in translated:
                                translate_both(n)
                        translated = []
                    else:
                        safe_send({"engine": "interim", "text": tr.strip()})
                        if len(pieces) >= 2:
                            for p in pieces[:-1]:
                                n = p.strip()
                                if n and n not in translated:
                                    translated.append(n)
                                    translate_both(n)
        except Exception as e:
            safe_send({"engine": "error", "text": str(e)[:120]})
            if stop_flag["stop"]:
                break


@app.route("/tunnel-url")
def tunnel_url():
    """공개 주소 반환. 우선순위: 자동 ngrok > ngrok-domain.txt > cloudflare."""
    import re
    base = APP_DIR

    # 1) 자동 시작된 ngrok URL
    if _ngrok_url:
        return jsonify({"url": _ngrok_url})

    # 2) 수동 설정된 ngrok 고정 도메인 파일
    domain_path = os.path.join(base, "ngrok-domain.txt")
    try:
        if os.path.exists(domain_path):
            with open(domain_path, "r", encoding="utf-8", errors="ignore") as f:
                domain = f.read().strip()
            if domain:
                return jsonify({"url": "https://" + domain})
    except Exception:
        pass

    # 3) cloudflare 임시 주소(tunnel.log)
    log_path = os.path.join(base, "tunnel.log")
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        matches = re.findall(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", text)
        if matches:
            return jsonify({"url": matches[-1]})
    except Exception:
        pass
    return jsonify({"url": ""})


# ===== 문장 끊기 — 자막을 언제 내보낼지 정한다 =====
# 세 가지 신호를 함께 쓴다. 앞의 것이 걸리면 뒤의 것은 필요 없다.
#  1) 음성 활동 종료(VAD): 구글이 "말이 멈췄다"고 알려주면 그 자리에서 끊는다.
#     추측이 아니라 인식기가 직접 준 신호라 가장 정확하다.
#  2) LocalAgreement: 연속 두 번의 인식 결과가 '일치하는 앞부분'만 확정한다.
#     (ufal/whisper_streaming 방식) 곧 바뀔 글자를 미리 번역하는 일이 없어진다.
#  3) 시간 강제 끊기: 위 둘이 오래 안 걸릴 때를 위한 안전망. 스페인어처럼
#     마침표가 안 붙는 언어에서 송출 화면이 1분씩 멈추는 것을 막는다.
_SENT_END_CHARS = ".?!。？！…"
_SENT_SPLIT = re.compile(r'(?<=[\.\?\!。？！…])\s*')


def split_sentences(t):
    """문장부호 뒤에서 자른다. 부호는 앞 문장에 포함해 반환."""
    return [p for p in _SENT_SPLIT.split(t) if p.strip()]


class Segmenter:
    """인식 중간 결과를 받아 '번역해도 안전한 조각'만 돌려준다.

    한 번의 인식 스트림마다 하나씩 새로 만든다.
    sent_len은 '이번 발화에서 이미 내보낸 글자 수'이며, 발화가 끝나면 0으로 돌아간다.
    """

    FORCE_SEC = 5.0        # 이 시간 넘게 자막이 안 나가면
    FORCE_MIN_CHARS = 30   # 그리고 이만큼 쌓였으면 강제로 끊는다
    REWIND_MAX = 40        # 최종 결과가 앞부분을 고쳤을 때 되돌릴 수 있는 최대 글자 수
    SLOW_INTERIM_SEC = 1.5  # 중간 결과가 이보다 드물게 오면 '느린 엔진'으로 본다

    def __init__(self, now):
        self.sent_len = 0
        self.prev = ""              # 직전 중간 결과 (LocalAgreement 비교용)
        self.prev_at = now          # 직전 중간 결과가 온 시각
        self.emitted = ""           # 이미 내보낸 원문 (최종 결과와 대조용)
        self.last_sent_at = now
        self.recent = _deque(maxlen=8)   # 같은 발화 안에서의 중복 방지

    # ── 내부 도구 ──
    @staticmethod
    def _common_len(a, b):
        n = 0
        for x, y in zip(a, b):
            if x != y:
                break
            n += 1
        return n

    @classmethod
    def _agreed(cls, prev, cur):
        """직전 결과와 현재 결과가 일치하는 앞부분을 단어 경계까지 잘라 돌려준다.
        마지막 단어는 아직 바뀔 수 있으므로 떼어낸다."""
        n = cls._common_len(prev, cur)
        if n == 0:
            return ""
        if n < len(cur):
            cut = cur.rfind(" ", 0, n)
            return cur[:cut] if cut > 0 else ""
        return cur[:n]

    @staticmethod
    def _last_sentence_end(t):
        """마지막 문장부호 바로 다음 위치. 완성된 문장이 없으면 0."""
        for i in range(len(t) - 1, -1, -1):
            if t[i] in _SENT_END_CHARS:
                return i + 1
        return 0

    @staticmethod
    def force_cut(t):
        """쉼표류가 뒤쪽에 있으면 거기서, 없으면 마지막 띄어쓰기에서 끊는다."""
        for mark in (",", ";", ":", "、", "，"):
            i = t.rfind(mark)
            if i >= len(t) * 0.4:
                return t[:i + 1]
        i = t.rfind(" ")
        if i >= len(t) * 0.5:
            return t[:i]
        return t   # 끊을 자리가 없으면 통째로

    @staticmethod
    def _worth_translating(s):
        # 글자가 하나도 없는 조각(마침표만, 공백만)은 번역에 보내지 않는다.
        # 보내면 Claude가 "번역할 내용이 없습니다" 같은 문장을 자막으로 내보낸다.
        return len(s) >= 2 and re.search(r"\w", s) is not None

    def _split_new(self, raw):
        out = []
        for p in split_sentences(raw):
            s = p.strip()
            if s and s not in self.recent and self._worth_translating(s):
                self.recent.append(s)
                out.append(s)
        return out

    def _take(self, transcript, cut, now):
        """transcript에서 앞으로 cut글자를 확정해 문장 단위로 돌려준다."""
        raw = transcript[self.sent_len:self.sent_len + cut]
        self.emitted += raw
        self.sent_len += cut
        self.last_sent_at = now
        return self._split_new(raw)

    # ── 바깥에서 부르는 것 ──
    def on_interim(self, transcript, now):
        # LocalAgreement의 값어치는 중간 결과가 얼마나 자주 오느냐에 달려 있다.
        #   V1(latest_long) : 0.25초마다 → 한 번 더 기다려도 손해가 없다
        #   chirp_3         : 6초마다    → 두 번 기다리면 12초 지연이 된다
        # 드물게 오는 엔진에서는 이번 결과를 그대로 믿는다. 드물게 온다는 것은
        # 인식기가 이미 한 번 정리해서 보냈다는 뜻이다.
        slow = (now - self.prev_at) >= self.SLOW_INTERIM_SEC
        confirmed = transcript if slow else self._agreed(self.prev, transcript)
        self.prev = transcript
        self.prev_at = now
        # 2) 두 번 연속 일치한 부분에 완성된 문장이 있으면 그것부터 내보낸다
        if len(confirmed) > self.sent_len:
            cut = self._last_sentence_end(confirmed[self.sent_len:])
            if cut:
                return self._take(transcript, cut, now)
        # 3) 안전망 — 오래 안 나갔으면 확정 여부와 상관없이 끊어 보낸다.
        #    (스페인어처럼 마침표가 안 붙고 인식 결과도 계속 흔들리는 경우)
        if (now - self.last_sent_at >= self.FORCE_SEC
                and len(transcript) > self.sent_len):
            pending = transcript[self.sent_len:]
            if len(pending.strip()) >= self.FORCE_MIN_CHARS:
                chunk = self.force_cut(pending)
                if chunk.strip():
                    return self._take(transcript, len(chunk), now)
        return []

    def on_speech_end(self, now):
        """1) 구글이 '말이 멈췄다'고 알림 → 남은 것을 전부 내보낸다."""
        if len(self.prev) <= self.sent_len:
            return []
        return self._take(self.prev, len(self.prev) - self.sent_len, now)

    def on_final(self, transcript, now):
        # 인식기는 발화가 끝날 때 앞부분 표현을 통째로 고쳐 쓰기도 한다.
        # 갈라지는 지점까지 되돌리되 REWIND_MAX 글자까지만 되돌린다.
        # 많이 어긋났는데 그대로 되돌리면, 이미 나갔던 자막 서너 문장이
        # 20초쯤 뒤에 통째로 다시 나온다(실제로 겪음). 조금 빠뜨리는 편이 낫다.
        start = self.sent_len
        if not transcript.startswith(self.emitted):
            n = self._common_len(self.emitted, transcript)
            if self.sent_len - n <= self.REWIND_MAX:
                start = n
        out = self._split_new(transcript[start:]) if len(transcript) > start else []
        self.sent_len = 0
        self.prev = ""
        self.prev_at = now
        self.emitted = ""
        self.last_sent_at = now
        self.recent.clear()
        return out


@sock.route("/audio")
def audio_socket(ws):
    """브라우저에서 16kHz PCM 오디오를 받아 Google Speech로 스트리밍 인식 → 번역."""
    from google.cloud import speech

    # 첫 메시지: 설정(JSON)
    cfg_raw = ws.receive()
    cfg = json.loads(cfg_raw)
    src_code = STT_LANG.get(cfg.get("source_lang_code"), "ko-KR")
    source_name = STT_TO_NAME.get(cfg.get("source_lang_code"), "Korean")
    target_lang = cfg.get("target_lang", "English")

    # 인식 힌트(speech adaptation)
    # 용어집·설교 원고는 모두 '한국어' 단어 목록이다. 원어가 스페인어 등 다른
    # 언어일 때 이걸 힌트로 주면 인식 정확도와 자동 문장부호가 오히려 나빠진다.
    ko_source = src_code.startswith("ko")
    speech_contexts = []
    # 1) 교회 용어집 — 모든 항목을 높은 가중치로(고유명사 사전)
    if glossary_terms and ko_source:
        speech_contexts.append(speech.SpeechContext(phrases=glossary_terms[:4000], boost=20.0))
    # 2) 오늘 설교 원고에서 자주 나오는 단어 — 보조 힌트
    if sermon_context and ko_source:
        import re as _re2
        from collections import Counter
        words = _re2.findall(r"[가-힣]{2,}", sermon_context)
        common = [w for w, _ in Counter(words).most_common(300)]
        if common:
            speech_contexts.append(speech.SpeechContext(phrases=common, boost=12.0))

    speech_client = speech.SpeechClient()
    recog_config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=16000,
        language_code=src_code,
        enable_automatic_punctuation=True,
        model="latest_long",        # 긴 발화에 적합한 최신 모델
        use_enhanced=True,          # 고품질(enhanced) 모델 사용
        speech_contexts=speech_contexts,
    )
    streaming_config = speech.StreamingRecognitionConfig(
        config=recog_config,
        interim_results=True,
        # 말이 멈추면 SPEECH_ACTIVITY_END 이벤트가 온다 → 그 자리에서 문장을 끊는다.
        # (voice_activity_timeout은 켜지 않는다. 그건 침묵이 길면 스트림을 닫아버려
        #  찬양·기도 중에 인식이 끊긴다.)
        enable_voice_activity_events=True,
    )

    audio_q = queue.Queue()
    stop_flag = {"stop": False}

    # 브라우저 → 오디오 수신 스레드
    def receiver():
        try:
            while True:
                data = ws.receive()
                if data is None:
                    break
                if isinstance(data, (bytes, bytearray)):
                    audio_q.put(bytes(data))
        except Exception:
            pass
        finally:
            stop_flag["stop"] = True
            audio_q.put(None)

    recv_thread = threading.Thread(target=receiver, daemon=True)
    recv_thread.start()

    def request_gen():
        # Google 스트림은 약 5분 제한 → 호출부에서 재시작
        while not stop_flag["stop"]:
            chunk = audio_q.get()
            if chunk is None:
                return
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

    # 이 연결의 세션 ID (마이크를 끄면 무효화되어 대기 중 번역이 폐기됨)
    global _current_session
    with _session_lock:
        _current_session += 1
        my_session = _current_session
    reset_context()   # 새 마이크 세션: 이전 발화 문맥 초기화

    import time as _time

    _SPEECH_END = speech.StreamingRecognizeResponse.SpeechEventType.SPEECH_ACTIVITY_END

    # 5분 제한 대응: 스트림이 끝나면 다시 연결 (음성 큐는 유지)
    while not stop_flag["stop"]:
        try:
            # 스트림마다 새로 만든다. 글자 위치(sent_len)가 그 스트림 기준이라
            # 스트림이 바뀌면 이어 쓸 수 없다.
            seg = Segmenter(_time.time())
            responses = speech_client.streaming_recognize(streaming_config, request_gen())
            for response in responses:
                now = _time.time()
                # 1) 말이 멈췄다는 신호 — 남은 조각을 바로 내보낸다
                if response.speech_event_type == _SPEECH_END:
                    for s in seg.on_speech_end(now):
                        enqueue_translation(s, source_name, my_session)
                # 구글은 한 응답에 두 가지를 함께 보낸다.
                #   results[0] = 지금까지의 전체 문장 (안정도 0.9 수준)
                #   results[1] = 아직 흔들리는 뒷조각 (안정도 0.01 수준)
                # 뒷조각까지 문장 끊기에 넣으면 앞뒤가 뒤섞여 아무것도 확정하지
                # 못한다. 화면에 보여줄 것도 [0]이므로 [0]만 쓴다.
                if response.results:
                    result = response.results[0]
                    transcript = result.alternatives[0].transcript
                    if transcript.strip():
                        if result.is_final:
                            operator_queue.put(("input", transcript.strip()))
                            for s in seg.on_final(transcript, now):
                                enqueue_translation(s, source_name, my_session)
                        else:
                            operator_queue.put(("interim", transcript.strip()))
                            for s in seg.on_interim(transcript, now):
                                enqueue_translation(s, source_name, my_session)
        except Exception as e:
            operator_queue.put(("stt_error", str(e)[:120]))
            if stop_flag["stop"]:
                break

    # 마이크 종료: 이 세션을 무효화해 대기 중인 번역 작업을 폐기
    with _session_lock:
        if _current_session == my_session:
            _current_session += 1


@app.route("/translate", methods=["POST"])
def translate():
    global last_input
    data = request.json
    text = data.get("text", "").strip()
    target_lang = data.get("target_lang", settings["target_lang"])
    source_lang = data.get("source_lang", settings["source_lang"])

    if not text:
        return jsonify({"error": "텍스트를 입력하세요."}), 400

    last_input = text
    operator_queue.put(("input", text))

    # 대표 언어 + 셀폰이 구독한 언어들로 번역 (수동 입력은 항상 실행)
    enqueue_translation(text, source_lang, None)

    return jsonify({"status": "started"})


@app.route("/upload", methods=["POST"])
def upload():
    global sermon_context
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "파일이 없습니다."}), 400

    filename = file.filename.lower()
    try:
        if filename.endswith(".txt"):
            sermon_context = file.read().decode("utf-8", errors="ignore")

        elif filename.endswith(".pdf"):
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(file.read()))
            sermon_context = "\n".join(page.extract_text() or "" for page in reader.pages)

        elif filename.endswith(".docx"):
            from docx import Document
            doc = Document(io.BytesIO(file.read()))
            sermon_context = "\n".join(p.text for p in doc.paragraphs)

        else:
            return jsonify({"error": "지원 형식: .txt, .pdf, .docx"}), 400

        preview = sermon_context[:200].replace("\n", " ")
        return jsonify({"status": "ok", "chars": len(sermon_context), "preview": preview})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/upload-clear", methods=["POST"])
def upload_clear():
    global sermon_context
    sermon_context = ""
    return jsonify({"status": "cleared"})


def _extract_text(file):
    name = file.filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(file.read()))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if name.endswith(".docx"):
        from docx import Document
        doc = Document(io.BytesIO(file.read()))
        return "\n".join(p.text for p in doc.paragraphs)
    return file.read().decode("utf-8", errors="ignore")


@app.route("/upload-glossary", methods=["POST"])
def upload_glossary():
    """교회 용어집 업로드 — 기존 용어집에 병합(중복 제거)하여 저장."""
    global glossary_terms
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "파일이 없습니다."}), 400
    try:
        text = _extract_text(file)
        # 기존 용어 유지 + 새 용어 추가 (순서 보존, 중복 제거)
        seen = set(glossary_terms)
        merged = list(glossary_terms)
        new_count = 0
        for ln in text.splitlines():
            t = ln.strip()
            if t and t not in seen:
                seen.add(t)
                merged.append(t)
                new_count += 1
        glossary_terms = merged
        # glossary.txt에 저장(다음 실행에도 유지)
        path = os.path.join(APP_DIR, "glossary.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(merged))
        return jsonify({"status": "ok", "count": len(merged), "added": new_count,
                        "preview": ", ".join(merged[:15])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/glossary-info")
def glossary_info():
    return jsonify({"count": len(glossary_terms),
                    "preview": ", ".join(glossary_terms[:15])})


def _save_mapping():
    path = os.path.join(APP_DIR, "mapping.txt")
    with open(path, "w", encoding="utf-8") as f:
        for k, v in translation_mapping.items():
            f.write(f"{k}={v}\n")


def _mapping_payload():
    items = [{"k": k, "v": v} for k, v in translation_mapping.items()]
    preview = ", ".join(f"{k}→{v}" for k, v in list(translation_mapping.items())[:5])
    return {"status": "ok", "count": len(items), "preview": preview, "items": items}


@app.route("/upload-mapping", methods=["POST"])
def upload_mapping():
    """번역 대응표 업로드 — 한글=English 형식, 기존 항목에 병합."""
    global translation_mapping
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "파일이 없습니다."}), 400
    try:
        text = _extract_text(file)
        for line in text.splitlines():
            line = line.strip()
            if "=" in line and line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k and v:
                    translation_mapping[k] = v
        _save_mapping()
        return jsonify(_mapping_payload())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/add-mapping", methods=["POST"])
def add_mapping():
    """단어를 직접 입력해 대응표에 추가/수정."""
    global translation_mapping
    data = request.get_json(force=True)
    k = (data.get("korean") or "").strip()
    v = (data.get("english") or "").strip()
    if not k or not v:
        return jsonify({"error": "단어와 번역을 모두 입력하세요."}), 400
    translation_mapping[k] = v
    _save_mapping()
    return jsonify(_mapping_payload())


@app.route("/remove-mapping", methods=["POST"])
def remove_mapping():
    """대응표에서 항목 삭제."""
    global translation_mapping
    data = request.get_json(force=True)
    k = (data.get("korean") or "").strip()
    if k in translation_mapping:
        del translation_mapping[k]
        _save_mapping()
    return jsonify(_mapping_payload())


@app.route("/mapping-info")
def mapping_info():
    return jsonify(_mapping_payload())


@app.route("/viewer-count")
def viewer_count():
    """폰으로 통역을 보고 있는 접속자 수(언어별 포함)."""
    return jsonify(hub.phone_stats())


def _vtuple(v):
    """'1.10' 같은 버전을 숫자로 비교 가능한 형태로."""
    try:
        return tuple(int(x) for x in str(v).strip().lstrip("vV").split("."))
    except Exception:
        return (0,)


@app.route("/check-update")
def check_update():
    """GitHub 최신 릴리스와 현재 버전을 비교해 새 버전이 있으면 알려준다.
    네트워크가 안 되거나 릴리스가 없으면 조용히 '최신'으로 응답(방해 금지)."""
    import urllib.request
    try:
        req = urllib.request.Request(UPDATE_API, headers={"User-Agent": "LiveWord"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        latest = str(data.get("tag_name", "")).lstrip("vV")
        if latest and _vtuple(latest) > _vtuple(APP_VERSION):
            return jsonify(update=True, current=APP_VERSION, latest=latest,
                           url=data.get("html_url", ""),
                           notes=(data.get("body") or "")[:300])
    except Exception:
        pass
    return jsonify(update=False, current=APP_VERSION)


@app.route("/settings", methods=["GET", "POST"])
def update_settings():
    global settings
    if request.method == "POST":
        data = request.json
        settings.update({k: v for k, v in data.items() if k in settings})
        hub.broadcast(("__settings__", settings))
    return jsonify(settings)


@app.route("/clear-display", methods=["POST"])
def clear_display():
    """송출창·셀폰의 누적 자막을 모두 지운다(전체 초기화)."""
    global last_input, last_output
    hub.broadcast("__reset__")            # 모든 송출창/셀폰
    operator_queue.put(("reset", ""))     # 운영자 미리보기
    reset_context()                       # 문맥 초기화
    last_input = ""
    last_output = ""
    return jsonify(ok=True)


# Cloudflare 등 프록시의 버퍼링을 깨기 위한 초기 패딩(2KB 주석)
_SSE_PADDING = ":" + (" " * 2048) + "\n\n"


@sock.route("/ws-display")
def ws_display(ws):
    """자막 송출용 WebSocket. 송출창(대표 언어)+셀폰(선택 언어) 공용.
    셀폰은 ?lang=English 처럼 언어를 지정하면 그 언어 자막만 받는다.
    lang이 없으면 대표 언어(운영자 선택)를 따라간다(송출창)."""
    lang = request.args.get("lang")
    q = hub.subscribe(lang) if lang else hub.subscribe_primary()
    try:
        ws.send(json.dumps({"type": "settings", "data": settings}))
        while True:
            try:
                item = q.get(timeout=10)
            except queue.Empty:
                ws.send(json.dumps({"type": "ping"}))
                continue
            if isinstance(item, tuple) and item[0] == "__settings__":
                msg = {"type": "settings", "data": item[1]}
            elif isinstance(item, tuple) and item[0] == "__audio__":
                msg = {"type": "audio", "data": item[1]}
            elif item == "__reset__":
                msg = {"type": "reset"}
            elif item == "__clear__":
                msg = {"type": "clear"}
            elif item == "__done__":
                msg = {"type": "done"}
            else:
                msg = {"type": "chunk", "data": item}
            ws.send(json.dumps(msg))
    except Exception:
        pass
    finally:
        hub.unsubscribe(q)


@app.route("/stream")
def stream():
    q = hub.subscribe_primary()

    def event_stream():
        yield _SSE_PADDING  # 버퍼 강제 비우기
        try:
            while True:
                try:
                    item = q.get(timeout=1)
                except queue.Empty:
                    yield ": ping\n\n"  # 하트비트(연결 유지 + 버퍼 flush)
                    continue
                if isinstance(item, tuple) and item[0] == "__settings__":
                    yield f"data: __settings__{json.dumps(item[1])}\n\n"
                elif isinstance(item, tuple) and item[0] == "__audio__":
                    continue  # 송출창(SSE)에는 음성 전송 안 함 — 셀폰 전용
                elif item == "__reset__":
                    yield "data: __reset__\n\n"
                elif item == "__clear__":
                    yield "data: __clear__\n\n"
                elif item == "__done__":
                    yield "data: __done__\n\n"
                else:
                    escaped = item.replace("\n", "\\n")
                    yield f"data: {escaped}\n\n"
        finally:
            hub.unsubscribe(q)

    return Response(event_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/operator-stream")
def operator_stream():
    q = operator_queue.subscribe()

    def event_stream():
        yield _SSE_PADDING
        yield f"data: {json.dumps({'type': 'settings', 'data': settings})}\n\n"
        try:
            while True:
                try:
                    event_type, data = q.get(timeout=1)
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps({'type': event_type, 'data': data})}\n\n"
        finally:
            operator_queue.unsubscribe(q)

    return Response(event_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── 설정 화면 (첫 실행 시 API 키 입력) ───────────────────────────────────────

@app.route("/setup")
def setup():
    return send_from_directory(BUNDLE_DIR, "setup.html")


@app.route("/setup-status")
def setup_status():
    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_ngrok_token = bool(os.environ.get("NGROK_AUTHTOKEN"))
    ngrok_domain = os.environ.get("NGROK_DOMAIN", "")
    if not has_anthropic or not has_ngrok_token:
        env_path = os.path.join(APP_DIR, ".env")
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8-sig") as f:
                content = f.read()
            if not has_anthropic:
                has_anthropic = "ANTHROPIC_API_KEY=" in content
            if not has_ngrok_token:
                has_ngrok_token = "NGROK_AUTHTOKEN=" in content
    has_google = os.path.exists(os.path.join(APP_DIR, "google-key.json"))
    return jsonify(
        has_anthropic_key=has_anthropic,
        has_google_key=has_google,
        has_ngrok_token=has_ngrok_token,
        ngrok_domain=ngrok_domain,
        ngrok_url=_ngrok_url,
    )


@app.route("/setup-save", methods=["POST"])
def setup_save():
    data = request.get_json(force=True)
    anthropic_key = (data.get("anthropic_key") or "").strip()
    ngrok_token   = (data.get("ngrok_token") or "").strip()
    ngrok_domain  = (data.get("ngrok_domain") or "").strip()

    if not anthropic_key and not ngrok_token and not ngrok_domain:
        return jsonify(ok=False, error="저장할 값이 없습니다")
    if anthropic_key and not anthropic_key.startswith("sk-"):
        return jsonify(ok=False, error="올바른 Anthropic 키 형식이 아닙니다 (sk-ant-... 로 시작해야 함)")

    try:
        env_path = os.path.join(APP_DIR, ".env")
        # 기존 .env 읽어서 해당 키만 교체
        env_vars = {}
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8-sig") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        env_vars[k.strip()] = v.strip()
        if anthropic_key:
            env_vars["ANTHROPIC_API_KEY"] = anthropic_key
            os.environ["ANTHROPIC_API_KEY"] = anthropic_key
            global client
            client = None
        if ngrok_token:
            env_vars["NGROK_AUTHTOKEN"] = ngrok_token
            os.environ["NGROK_AUTHTOKEN"] = ngrok_token
        if ngrok_domain:
            env_vars["NGROK_DOMAIN"] = ngrok_domain
            os.environ["NGROK_DOMAIN"] = ngrok_domain

        with open(env_path, "w", encoding="utf-8") as f:
            for k, v in env_vars.items():
                f.write(f"{k}={v}\n")

        # ngrok 토큰이 새로 저장됐으면 터널 시작
        if ngrok_token and not _ngrok_url:
            threading.Thread(target=_start_ngrok, daemon=True).start()

        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/setup-upload-google-key", methods=["POST"])
def setup_upload_google_key():
    f = request.files.get("file")
    if not f:
        return jsonify(ok=False, error="파일이 없습니다")
    try:
        content = f.read()
        parsed = json.loads(content)
        if parsed.get("type") != "service_account":
            return jsonify(ok=False, error="서비스 계정 JSON 파일이 아닙니다")
        dest = os.path.join(APP_DIR, "google-key.json")
        with open(dest, "wb") as out:
            out.write(content)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = dest
        return jsonify(ok=True)
    except json.JSONDecodeError:
        return jsonify(ok=False, error="JSON 형식이 올바르지 않습니다")
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/setup-test-anthropic", methods=["POST"])
def setup_test_anthropic():
    data = request.get_json(force=True)
    key = (data.get("api_key") or "").strip() or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return jsonify(ok=False, error="API 키를 입력해 주세요")
    try:
        import anthropic as _anthropic
        test_client = _anthropic.Anthropic(api_key=key)
        msg = test_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": "Hi"}]
        )
        return jsonify(ok=True, model=msg.model)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/setup-test-google", methods=["POST"])
def setup_test_google():
    try:
        from google.cloud import speech
        client_stt = speech.SpeechClient()
        # 간단한 인식 요청으로 인증 확인 (빈 오디오는 오류지만 인증 성공 여부만 체크)
        client_stt.recognize(
            config=speech.RecognitionConfig(
                encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=16000,
                language_code="ko-KR",
            ),
            audio=speech.RecognitionAudio(content=b"\x00" * 32000),
        )
        return jsonify(ok=True)
    except Exception as e:
        err = str(e)
        # 빈 오디오로 인한 오류는 인증 성공으로 간주
        if "no speech" in err.lower() or "audio" in err.lower() or "empty" in err.lower():
            return jsonify(ok=True)
        if "credentials" in err.lower() or "permission" in err.lower() or "auth" in err.lower():
            return jsonify(ok=False, error="인증 실패: " + err)
        # 그 외 오류는 연결 자체는 된 것
        return jsonify(ok=True)


def _open_app_window(url):
    """운영자 화면을 Edge/Chrome '앱 모드'(주소창·탭 없는 독립 창)로 연다.
    Edge/Chrome이 없으면 기본 브라우저로 폴백."""
    import subprocess, webbrowser
    # Windows에 기본 내장된 Edge 우선, 그다음 Chrome
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    ]
    for exe in candidates:
        if os.path.exists(exe):
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1360,900"])
                return
            except Exception:
                pass
    webbrowser.open(url)  # 폴백: 일반 브라우저


def _open_browser():
    """서버가 뜬 뒤 운영자 창을 자동으로 연다. 설정이 안 됐으면 /setup, 됐으면 /operator."""
    import time
    time.sleep(1.5)  # 서버 기동 대기
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    url = "http://localhost:5000/" + ("operator" if has_key else "setup")
    print(f"  앱 창 열기: {url}")
    _open_app_window(url)


def _already_running():
    """포트 5000에 이미 서버가 떠 있는지 확인."""
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", 5000), timeout=1)
        s.close()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    if _already_running():
        print("프로그램이 이미 실행 중입니다. 기존 화면을 엽니다.")
        _open_app_window("http://localhost:5000/operator")
        sys.exit(0)
    print("=" * 50)
    print("  LiveWord 서버 시작")
    print(f"  설정 파일 위치: {APP_DIR}")
    print("  설정 화면:       http://localhost:5000/setup")
    print("  운영자 대시보드: http://localhost:5000/operator")
    print("=" * 50)
    threading.Thread(target=_open_browser, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, threaded=True)
