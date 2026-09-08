"""
Translation server with operator dashboard.
"""
import os
import io
import json
import queue
import re
import threading
import anthropic
from flask import Flask, request, jsonify, Response, send_from_directory
from flask_sock import Sock
from dotenv import load_dotenv

load_dotenv()

# Google Cloud 인증: 폴더 내 google-key.json 자동 사용
_key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "google-key.json")
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


client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

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
    # 음성인식 엔진: "v1" = 지금까지 쓰던 latest_long, "chirp3" = V2 모델,
    # "gemini_live" = Gemini 3.5 Transcribe Live (GEMINI_API_KEY 필요)
    # 기본값은 gemini_live. 운영자 화면에서 바꿔 같은 설교로 비교할 수 있다.
    # GEMINI_API_KEY가 없는 PC에서는 아래 audio_socket이 v1으로 되돌린다.
    "stt_engine": "gemini_live",
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
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "glossary.txt")
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [ln.strip() for ln in f.read().splitlines()]
        glossary_terms = [ln for ln in lines if ln]
    except Exception:
        glossary_terms = []


load_glossary()

# 번역 대응표 (한글=English 형식)
#   translation_mapping — 항상 쓰는 고정 대응표 (mapping.txt)
#   weekly_mapping      — 그날 설교에만 쓰는 임시 대응표 (mapping_주간.txt)
# 교회는 같은 말씀으로 주일에 두 번 예배한다. 1부에서 잘못 나간 곳을 검토해
# 임시 대응표에 넣고 2부에 쓰고, 그 주가 끝나면 비운다. 이렇게 하면 대응표가
# 해마다 쌓여 문장마다 프롬프트에 실리는 문제(500개면 예배당 $23)가 없다.
translation_mapping = {}
weekly_mapping = {}

WEEKLY_MAPPING_FILE = "mapping_주간.txt"


def effective_mapping():
    """번역에 실제로 쓰는 대응표. 그날 것이 고정보다 우선한다."""
    merged = dict(translation_mapping)
    merged.update(weekly_mapping)
    return merged


_HANGUL = re.compile(r"[가-힣]")


def _apply_recognition_fixes(text):
    """대응표 중 값이 '한글'인 항목은 인식 교정으로 쓴다.

    예) 데모 → 대목   구글 지경 → 큰 구렁
    들린 글자를 옳은 낱말로 바꿔 넣으면 번역은 문맥에 맞게 알아서 된다.
    영어 번역을 강제로 지정하는 것보다 조사·어미가 자연스럽다.
    (값이 영어인 항목은 지금까지처럼 프롬프트로 넘겨 번역을 고정한다)

    긴 낱말부터 바꾼다 — 짧은 것이 먼저 걸리면 긴 항목이 영향을 받는다.
    돌려주는 것: (바뀐 글, [(들린말, 옳은말), ...])
    """
    fixed, used = text, []
    items = [(k, v) for k, v in effective_mapping().items()
             if k and v and _HANGUL.search(v)]
    for k, v in sorted(items, key=lambda kv: -len(kv[0])):
        if k in fixed:
            fixed = fixed.replace(k, v)
            used.append((k, v))
    return fixed, used


def _weekly_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), WEEKLY_MAPPING_FILE)


def load_weekly_mapping():
    global weekly_mapping
    out = {}
    try:
        path = _weekly_path()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and line:
                        k, v = line.split("=", 1)
                        out[k.strip()] = v.strip()
    except Exception:
        out = {}
    weekly_mapping = out


def _save_weekly_mapping():
    with open(_weekly_path(), "w", encoding="utf-8") as f:
        for k, v in weekly_mapping.items():
            f.write("%s=%s\n" % (k, v))


def load_mapping():
    global translation_mapping
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mapping.txt")
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
load_weekly_mapping()


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


_diag_last = {}
_diag_lock = threading.Lock()


def _log_diag(key, msg, min_gap=10.0):
    """자막 멈춤의 원인을 가리기 위한 진단 기록.

    실제 예배에서만 일어나는 현상이라 로그로 남겨야 알 수 있다.
    같은 종류는 min_gap 초에 한 번만 남긴다 (로그가 넘치지 않게).
    기록만 하고 동작은 바꾸지 않는다."""
    import time as _t
    now = _t.time()
    with _diag_lock:
        if now - _diag_last.get(key, 0) < min_gap:
            return
        _diag_last[key] = now
    try:
        import datetime
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "로그.txt"), "a", encoding="utf-8") as f:
            f.write("[%s] 진단: %s\n" % (datetime.datetime.now().strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _log_translation(src, out, usage=None, warn=None, total=0.0, fixes=None):
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "로그.txt")
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] 입력: {src}\n")
            if fixes:
                f.write("[%s] 교정: %s\n" % (
                    ts, ", ".join("%s→%s" % (k, v) for k, v in fixes)))
            f.write(f"[{ts}] 번역: {out}\n")
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
    # 인식 교정(한글 → 한글)은 번역 전에 글자를 바꿔 넣는다
    text, _fixes = _apply_recognition_fixes(text)
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
    # 값이 한글인 항목은 위에서 이미 글자를 바꿨으므로 프롬프트에 넣지 않는다
    _mapping = {k: v for k, v in effective_mapping().items() if not _HANGUL.search(v)}
    if _mapping:
        pairs = "\n".join(f"  {k} → {v}" for k, v in _mapping.items())
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
    with client.messages.stream(
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
        # print는 창 없는 .exe에서 사라진다. 예배 중에 바로 보이도록
        # 운영자 화면으로 보낸다. (로그.txt에도 남는다)
        print(warn)
        operator_queue.put(("cost_warn", warn))
    if is_primary:
        last_output = out
        _log_translation(text, out, usage, warn, total, _fixes)
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
    return send_from_directory(".", "display.html")


@app.route("/operator")
def operator():
    return send_from_directory(".", "operator.html")


@app.route("/mobile")
@app.route("/m")            # QR에 담는 주소를 짧게 하려고 둔 별명 (QR 칸 40→37)
def mobile():
    return send_from_directory(".", "mobile.html")


@app.route("/guide")
def guide():
    return send_from_directory(".", "guide.html")


@app.route("/guide-en")
def guide_en():
    return send_from_directory(".", "guide-en.html")


@app.route("/poster")
def poster():
    return send_from_directory(".", "poster.html")


@app.route("/compare")
def compare():
    return send_from_directory(".", "compare.html")


def _translate_claude(text, target_lang, source_lang):
    system = (f"You are a professional church interpreter. Translate from {source_lang} into {target_lang}. "
              f"Output ONLY the translation.")
    m = client.messages.create(model="claude-opus-4-8", max_tokens=1024,
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
    """공개 주소 반환. ngrok 고정 도메인이 설정돼 있으면 우선 사용, 없으면 cloudflare 임시주소."""
    import re
    base = os.path.dirname(os.path.abspath(__file__))

    # 1) ngrok 고정 도메인 우선
    domain_path = os.path.join(base, "ngrok-domain.txt")
    try:
        if os.path.exists(domain_path):
            with open(domain_path, "r", encoding="utf-8", errors="ignore") as f:
                domain = f.read().strip()
            if domain:
                return jsonify({"url": "https://" + domain})
    except Exception:
        pass

    # 2) cloudflare 임시 주소(tunnel.log)
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


# ===== 음성인식 엔진 =====
# 두 엔진 모두 아래 형태의 이벤트만 내보낸다. 바깥(audio_socket)은 어느 엔진인지
# 몰라도 된다.
#   ("stream_start", "")  새 인식 스트림 시작 — 문장 끊기를 새로 시작하라
#   ("speech_end",   "")  말이 멈췄다
#   ("interim",   본문)   아직 말하는 중
#   ("final",     본문)   한 발화가 끝났다
#   ("error",     메시지) 인식 오류


def _google_project_id():
    """구글 프로젝트 ID를 찾는다 (V2 API에 필요).
    인증 파일은 두 종류다.
      - 서비스 계정 JSON        → project_id
      - gcloud 사용자 인증(ADC) → quota_project_id
    둘 다 없으면 google.auth가 아는 기본 프로젝트를 쓴다."""
    for key in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_QUOTA_PROJECT"):
        if os.environ.get(key):
            return os.environ[key]
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        pid = d.get("project_id") or d.get("quota_project_id")
        if pid:
            return pid
    except Exception:
        pass
    try:
        import google.auth
        return google.auth.default()[1] or ""
    except Exception:
        return ""


# 원고에서 힌트를 뽑을 때 떼어낼 조사. 긴 것부터 검사한다.
_HINT_JOSA = ("으로써", "으로서", "이라는", "이라고", "에서는", "에게서", "께서는",
              "라는", "라고", "에서", "에게", "으로", "까지", "부터", "보다", "마다",
              "조차", "처럼", "한테", "께서", "이나", "이란", "라도", "이여",
              "은", "는", "이", "가", "을", "를", "의", "에", "도", "와", "과",
              "로", "만", "야", "여", "께", "요")

# 흔해서 힌트로 값어치가 없는 말. 실제 예배 로그 27회분에서 가장 많이 나온
# 어절들을 보고, 고유명사·교회 용어가 아닌 것만 골라냈다.
_HINT_STOP = set("""
우리 저희 여러분 사람 사람들 이것 그것 저것 여기 거기 저기 지금 오늘 내일 어제
그때 자기 자신 서로 모두 무엇 누구 어디 언제 얼마 다시 그래서 그러나 그리고
하지만 그러면 그런데 그러니까 왜냐하면 이렇게 그렇게 저렇게 어떻게 이런 그런
저런 어떤 무슨 정말 진짜 아주 가장 매우 너무 조금 많이 함께 같이 물론 사실
이제 인제 먼저 나중 다음 모든 여러 많은 좋은 나쁜 같은 다른 새로운 이번 지난
온갖 때문 위해 통해 대해 관해 의해 따라 대신 만큼 정도 동안 사이 경우 방법
것들 부분 전체 자리 순간 한번 그것들 저것들
""".split())

# 용언 활용형 — 명사가 아니므로 힌트에 넣지 않는다.
_HINT_VERBISH = re.compile(
    r"(니다|세요|십시오|하는|하던|했던|하고|해서|하여|하지|하면|하며|해도|해야|"
    r"되는|되고|되어|되면|있는|있고|있어|없는|없고|없이|같은|같이|보면|보고|"
    r"보는|주는|주고|받는|받고|까요|나요|지요|네요|군요|거든|든지|"
    r"한다|된다|이다|있다|없다|였다)$")


def _hint_stem(word):
    """어절에서 조사를 떼어낸다. 한 글자만 남으면 부르는 쪽에서 버린다."""
    for josa in _HINT_JOSA:
        if word.endswith(josa):
            return word[:-len(josa)]
    return word


def _sermon_hint_terms(text, limit=300):
    """설교 원고에서 인식 힌트로 줄 말을 고른다.

    예전에는 빈도 상위 300 '어절'을 그대로 줬다. 그러면 실제로 뽑히는 것이
    '하는·있습니다·어떻게·이렇게'처럼 흔한 말이라 boost를 줘도 값어치가 없고,
    정작 필요한 고유명사는 원고에 한두 번만 나와서 상위에 들지 못했다.
    그래서 조사를 떼고 흔한 말과 용언을 걸러 명사만 남긴다.

    주의: '이사야→이사'처럼 조사와 같은 글자로 끝나는 고유명사는 잘못
    잘릴 수 있다. 성경 인명·지명은 glossary.txt에 따로 들어 있어
    그쪽 경로로 온전히 전달된다."""
    from collections import Counter
    cnt = Counter()
    for word in re.findall(r"[가-힣]{2,}", text):
        stem = _hint_stem(word)
        if len(stem) < 2 or stem in _HINT_STOP or _HINT_VERBISH.search(stem):
            continue
        cnt[stem] += 1
    return [w for w, _ in cnt.most_common(limit)]


def _stt_hint_phrases(src_code):
    """인식 힌트로 줄 단어들. 용어집·설교 원고는 한국어 목록이므로
    원어가 한국어일 때만 쓴다 (다른 언어에 주면 정확도가 오히려 나빠진다)."""
    if not src_code.startswith("ko"):
        return []
    out = list(glossary_terms)
    if sermon_context:
        out += _sermon_hint_terms(sermon_context)
    return out


def _stt_events_v1(src_code, audio_q, stop_flag):
    """지금까지 쓰던 엔진 — Speech-to-Text V1, latest_long + enhanced."""
    from google.cloud import speech

    phrases = _stt_hint_phrases(src_code)
    speech_contexts = []
    if phrases:
        speech_contexts.append(speech.SpeechContext(phrases=phrases[:4000], boost=20.0))

    client = speech.SpeechClient()
    recog_config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=16000,
        language_code=src_code,
        enable_automatic_punctuation=True,
        model="latest_long",        # 긴 발화에 적합한 모델
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
    END = speech.StreamingRecognizeResponse.SpeechEventType.SPEECH_ACTIVITY_END

    def request_gen():
        while not stop_flag["stop"]:
            chunk = audio_q.get()
            if chunk is None:
                return
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

    # 구글 스트림은 약 5분 제한 → 끝나면 다시 연결 (음성 큐는 유지)
    while not stop_flag["stop"]:
        try:
            yield ("stream_start", "")
            for response in client.streaming_recognize(streaming_config, request_gen()):
                if response.speech_event_type == END:
                    yield ("speech_end", "")
                # 한 응답에 results[0]=지금까지의 전체 문장(안정도 0.9),
                # results[1]=아직 흔들리는 뒷조각(0.01)이 함께 온다. [0]만 쓴다.
                if response.results:
                    r = response.results[0]
                    tr = r.alternatives[0].transcript
                    if tr.strip():
                        yield ("final" if r.is_final else "interim", tr)
        except Exception as e:
            yield ("error", str(e)[:120])
            if stop_flag["stop"]:
                break


def _stt_events_chirp3(src_code, audio_q, stop_flag):
    """새 엔진 — Speech-to-Text V2의 chirp_3.
    V1보다 좋은 점: 인식 정확도, 내장 잡음제거(반주·잔향), 그리고 custom_prompt로
    오늘 설교의 고유명사를 인식 단계에서 직접 알려줄 수 있다."""
    from google.api_core.client_options import ClientOptions
    from google.cloud.speech_v2 import SpeechClient
    from google.cloud.speech_v2.types import cloud_speech as t

    project = _google_project_id()
    if not project:
        yield ("error", "chirp3: 서비스 계정 JSON에서 프로젝트 ID를 못 읽었습니다")
        return

    # chirp_3는 us / eu 멀티리전에서 제공된다. 리전 전용 엔드포인트로 붙어야 한다.
    location = "us"
    client = SpeechClient(client_options=ClientOptions(
        api_endpoint="%s-speech.googleapis.com" % location))
    recognizer = "projects/%s/locations/%s/recognizers/_" % (project, location)

    features = t.RecognitionFeatures(enable_automatic_punctuation=True)
    # 오늘 설교 원고가 있으면 '무엇에 대한 설교인지'를 인식기에 직접 알려준다.
    if sermon_context:
        head = sermon_context[:1200].replace("\n", " ")
        features.custom_prompt_config = t.CustomPromptConfig(
            custom_prompt=("This is a live Christian sermon. Transcribe faithfully with "
                           "punctuation. Today's sermon covers: " + head))

    config = t.RecognitionConfig(
        explicit_decoding_config=t.ExplicitDecodingConfig(
            encoding=t.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000, audio_channel_count=1),
        language_codes=[src_code],
        model="chirp_3",
        features=features,
        # 본당 반주·잔향을 줄인다. (사람 목소리는 못 지운다)
        denoiser_config=t.DenoiserConfig(denoise_audio=True),
    )
    phrases = _stt_hint_phrases(src_code)[:1000]   # chirp_3는 1,000개 제한
    if phrases:
        config.adaptation = t.SpeechAdaptation(phrase_sets=[
            t.SpeechAdaptation.AdaptationPhraseSet(
                inline_phrase_set=t.PhraseSet(
                    phrases=[t.PhraseSet.Phrase(value=p, boost=20.0) for p in phrases]))])

    streaming_config = t.StreamingRecognitionConfig(
        config=config,
        streaming_features=t.StreamingRecognitionFeatures(
            interim_results=True, enable_voice_activity_events=True),
    )
    END = t.StreamingRecognizeResponse.SpeechEventType.SPEECH_ACTIVITY_END

    def request_gen():
        # V2는 첫 요청에 리코그나이저와 설정을 함께 보낸다
        yield t.StreamingRecognizeRequest(
            recognizer=recognizer, streaming_config=streaming_config)
        while not stop_flag["stop"]:
            chunk = audio_q.get()
            if chunk is None:
                return
            yield t.StreamingRecognizeRequest(audio=chunk)

    while not stop_flag["stop"]:
        try:
            yield ("stream_start", "")
            for response in client.streaming_recognize(requests=request_gen()):
                if response.speech_event_type == END:
                    yield ("speech_end", "")
                if response.results:
                    r = response.results[0]
                    if not r.alternatives:
                        continue
                    tr = r.alternatives[0].transcript
                    if tr.strip():
                        yield ("final" if r.is_final else "interim", tr)
        except Exception as e:
            yield ("error", "chirp3: " + str(e)[:110])
            if stop_flag["stop"]:
                break


async def _gemini_live_loop(audio_q, stop_flag, out_q, api_key):
    """Gemini Live 세션을 유지하며 (kind, text)를 out_q로 넘긴다.

    Live API는 asyncio 전용이고 이 프로그램의 나머지는 스레드+큐 구조다.
    그래서 오디오를 넘겨주는 스레드 하나(feeder)와 이 코루틴이 만나는 지점에
    asyncio 큐를 둔다. feeder는 재연결과 무관하게 하나만 돌아야 한다 —
    둘이 되면 같은 audio_q를 두 스레드가 나눠 먹어 음성이 새 세션에 안 간다.
    """
    import asyncio
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    aq = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def feeder():
        while not stop_flag["stop"]:
            chunk = audio_q.get()
            loop.call_soon_threadsafe(aq.put_nowait, chunk)
            if chunk is None:
                return

    threading.Thread(target=feeder, daemon=True).start()

    config = types.LiveConnectConfig(
        response_modalities=["TEXT"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
    )

    while not stop_flag["stop"]:
        try:
            async with client.aio.live.connect(
                    model="gemini-3.5-transcribe-live", config=config) as session:
                out_q.put(("stream_start", ""))

                async def sender():
                    while not stop_flag["stop"]:
                        chunk = await aq.get()
                        if chunk is None:
                            break
                        await session.send_realtime_input(
                            audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))
                    await session.send_realtime_input(audio_stream_end=True)

                async def receiver():
                    prev_text = ""
                    async for response in session.receive():
                        # 말이 멈췄다는 신호. V1의 SPEECH_ACTIVITY_END와 같은 자리다.
                        # 이게 없으면 남은 자막이 시간 강제 끊기까지 기다려서
                        # 문장 사이가 10초까지 벌어진다(실측).
                        va = getattr(response, "voice_activity", None)
                        if va and va.voice_activity_type == types.VoiceActivityType.ACTIVITY_END:
                            out_q.put(("speech_end", ""))
                        vs = getattr(response, "voice_activity_detection_signal", None)
                        if vs and vs.vad_signal_type == types.VadSignalType.VAD_SIGNAL_TYPE_EOS:
                            out_q.put(("speech_end", ""))
                        sc = response.server_content
                        if not sc:
                            continue
                        # interim이 실시간용이다. 문서의 기본 예제에는 이 필드가
                        # 없어서, 안 읽으면 2분에 4번밖에 안 온다.
                        # 이 글은 한 턴 동안 계속 누적된다. 누적이 끊기면(새 턴)
                        # 글자 위치가 어긋나므로 문장 끊기를 새로 시작해야 한다.
                        interim = getattr(sc, "interim_input_transcription", None)
                        if interim and interim.text:
                            text = interim.text
                            if prev_text and not text.startswith(prev_text[:20]):
                                # 끊기 전에 남은 것을 먼저 내보낸다. 안 그러면
                                # 턴이 바뀔 때마다 아직 안 나간 자막이 버려진다.
                                out_q.put(("speech_end", ""))
                                out_q.put(("stream_start", ""))
                            prev_text = text
                            out_q.put(("interim", text))
                        # input_transcription(최종)은 쓰지 않는다. V1의 is_final과
                        # 달리 발화 단위가 아니라 그 턴 전체를 다시 보내주는 것이어서,
                        # final로 넘기면 이미 나간 자막 여러 줄이 통째로 다시 나온다.

                send_task = asyncio.create_task(sender())
                recv_task = asyncio.create_task(receiver())
                _, pending = await asyncio.wait(
                    {send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
        except Exception as e:
            out_q.put(("error", "gemini_live: " + str(e)[:110]))
            if not stop_flag["stop"]:
                await asyncio.sleep(1.0)
    out_q.put(None)


def _stt_events_gemini_live(src_code, audio_q, stop_flag):
    """Gemini 3.5 Transcribe Live (Gemini Live API).

    2026-09-07 실측(8/30 1부 설교 2분): 자막 243회·평균 0.53초·첫 자막 1.31초로
    V1(164회·0.7초)보다 빈도가 높고 chirp_3(26회·4.7초)보다 훨씬 빠르다.
    단, 찬양(노래) 구간에서는 VAD가 발화로 인식하지 않아 자막이 하나도 나오지
    않는다. 찬양 시간에는 통역하지 않으므로 그대로 둔다.

    말이 멈췄다는 별도 신호(speech_end)는 이 API에 없다. 대신 발화가 끝나면
    input_transcription이 오므로 그것을 final로 넘긴다 — Segmenter는 final에서
    남은 것을 전부 내보내므로 결과가 같다.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        yield ("error", "gemini_live: GEMINI_API_KEY가 없습니다 (.env에 넣어야 합니다)")
        return

    import asyncio

    out_q = queue.Queue()

    def runner():
        asyncio.run(_gemini_live_loop(audio_q, stop_flag, out_q, api_key))

    threading.Thread(target=runner, daemon=True).start()

    while True:
        item = out_q.get()
        if item is None:
            return
        yield item


@sock.route("/audio")
def audio_socket(ws):
    """브라우저에서 16kHz PCM 오디오를 받아 Google Speech로 스트리밍 인식 → 번역."""
    # 첫 메시지: 설정(JSON)
    cfg_raw = ws.receive()
    cfg = json.loads(cfg_raw)
    src_code = STT_LANG.get(cfg.get("source_lang_code"), "ko-KR")
    source_name = STT_TO_NAME.get(cfg.get("source_lang_code"), "Korean")
    target_lang = cfg.get("target_lang", "English")

    audio_q = queue.Queue()
    stop_flag = {"stop": False}

    # 브라우저 → 오디오 수신 스레드
    def receiver():
        import time as _t
        last = _t.time()
        try:
            while True:
                data = ws.receive()
                if data is None:
                    break
                if isinstance(data, (bytes, bytearray)):
                    now = _t.time()
                    # 브라우저에서 음성이 끊겼는가 (한 조각 ≈ 0.085초 간격이 정상)
                    if now - last >= 1.0:
                        _log_diag("audio_gap",
                                  "브라우저에서 음성이 %.1f초 동안 오지 않았습니다" % (now - last))
                    last = now
                    audio_q.put(bytes(data))
                    # 인식이 못 따라가서 음성이 쌓이는가
                    n = audio_q.qsize()
                    if n >= 60:
                        _log_diag("audio_backlog",
                                  "음성 처리가 밀리고 있습니다 — 대기 %d조각(약 %.0f초분)"
                                  % (n, n * 0.085))
        except Exception:
            pass
        finally:
            stop_flag["stop"] = True
            audio_q.put(None)

    recv_thread = threading.Thread(target=receiver, daemon=True)
    recv_thread.start()

    # 이 연결의 세션 ID (마이크를 끄면 무효화되어 대기 중 번역이 폐기됨)
    global _current_session
    with _session_lock:
        _current_session += 1
        my_session = _current_session
    reset_context()   # 새 마이크 세션: 이전 발화 문맥 초기화

    import time as _time

    engine = settings.get("stt_engine", "gemini_live")
    # 키가 없으면 자막이 아예 안 나온다. 예배 중에 그러면 대안이 없으므로
    # 조용히 실패하지 않고 기본 엔진으로 돌아가고, 운영자 화면에 알린다.
    if engine == "gemini_live" and not os.environ.get("GEMINI_API_KEY", "").strip():
        engine = "v1"
        operator_queue.put(("cost_warn",
                            "GEMINI_API_KEY가 없어 기본 엔진(latest_long)으로 시작했습니다"))
    events = {"chirp3": _stt_events_chirp3,
              "gemini_live": _stt_events_gemini_live}.get(
        engine, _stt_events_v1)(src_code, audio_q, stop_flag)
    operator_queue.put(("stt_engine", engine))

    seg = Segmenter(_time.time())
    for kind, text in events:
        now = _time.time()
        if kind == "stream_start":
            # 스트림마다 새로 만든다. 글자 위치(sent_len)가 그 스트림 기준이라
            # 스트림이 바뀌면 이어 쓸 수 없다.
            seg = Segmenter(now)
        elif kind == "speech_end":
            for s in seg.on_speech_end(now):
                enqueue_translation(s, source_name, my_session)
        elif kind == "final":
            operator_queue.put(("input", text.strip()))
            for s in seg.on_final(text, now):
                enqueue_translation(s, source_name, my_session)
        elif kind == "interim":
            operator_queue.put(("interim", text.strip()))
            for s in seg.on_interim(text, now):
                enqueue_translation(s, source_name, my_session)
        elif kind == "error":
            operator_queue.put(("stt_error", text))

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


def _sermon_archive_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "설교원고")


def _save_sermon_script(filename, text):
    """올린 설교 원고를 파일로 남긴다.

    예전에는 sermon_context 변수에만 담아서 프로그램을 끄면 사라졌다.
    쌓아 두면 나중에 고유명사를 뽑아 용어집을 키우는 데 쓸 수 있다.
    저장에 실패해도 업로드 자체는 성공시킨다 (예배 중에 막히면 안 된다)."""
    if not text or not text.strip():
        return
    try:
        import datetime
        folder = _sermon_archive_dir()
        os.makedirs(folder, exist_ok=True)
        stem = os.path.splitext(os.path.basename(filename or "설교"))[0]
        stem = re.sub(r'[\/:*?"<>|]', "_", stem)[:60]
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
        with open(os.path.join(folder, stamp + "_" + stem + ".txt"),
                  "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        print("설교 원고 저장 실패:", e)


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

        _save_sermon_script(file.filename, sermon_context)
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
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "glossary.txt")
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
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mapping.txt")
    with open(path, "w", encoding="utf-8") as f:
        for k, v in translation_mapping.items():
            f.write(f"{k}={v}\n")


def _mapping_payload():
    items = [{"k": k, "v": v} for k, v in translation_mapping.items()]
    weekly = [{"k": k, "v": v} for k, v in weekly_mapping.items()]
    preview = ", ".join(f"{k}→{v}" for k, v in list(translation_mapping.items())[:5])
    return {"status": "ok", "count": len(items), "preview": preview,
            "items": items, "weekly": weekly, "weekly_count": len(weekly)}


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


@app.route("/add-weekly-mapping", methods=["POST"])
def add_weekly_mapping():
    """그날 설교용 임시 대응표에 추가. 예배 후 검토에서 고른 것이 여기로 들어온다."""
    data = request.get_json(force=True)
    k = (data.get("korean") or "").strip()
    v = (data.get("english") or "").strip()
    if not k or not v:
        return jsonify({"error": "단어와 번역을 모두 입력하세요."}), 400
    weekly_mapping[k] = v
    _save_weekly_mapping()
    return jsonify(_mapping_payload())


@app.route("/remove-weekly-mapping", methods=["POST"])
def remove_weekly_mapping():
    """그날 대응표에서 한 항목만 뺀다."""
    data = request.get_json(force=True)
    k = (data.get("korean") or "").strip()
    if k in weekly_mapping:
        del weekly_mapping[k]
        _save_weekly_mapping()
    return jsonify(_mapping_payload())


@app.route("/clear-weekly-mapping", methods=["POST"])
def clear_weekly_mapping():
    """그날 것을 전부 비운다. 고정 대응표(mapping.txt)는 건드리지 않는다."""
    weekly_mapping.clear()
    _save_weekly_mapping()
    return jsonify(_mapping_payload())


@app.route("/mapping-info")
def mapping_info():
    return jsonify(_mapping_payload())


# ===== 예배 후 검토 =====
# 로그.txt의 (입력 → 번역) 기록을 훑어 '음성인식이 잘못 알아들어 번역까지 틀어진 곳'을
# 찾는다. 찾기만 한다 — 대응표는 건드리지 않는다. 넣는 것은 운영자가 고른 것만이다.
# 대응표는 이후 모든 번역에 강제로 적용되므로, 자동으로 쌓으면 잘못된 한 줄이
# 그 뒤 모든 예배를 조용히 망가뜨린다.

_REVIEW_BATCH = 150      # 한 번에 검토할 문장 수 (실측상 이 크기에서 근거를 잘 찾는다)
_REVIEW_MAX = 1000


_REVIEW_HEADERS = {"잘못 인식된 말", "원래 말", "영어로는", "표적합", "근거", "근거(짧게)",
                   "잘못 들린 말", "원래 한국어 말", "근거 항목번호", "근거 문장 그대로"}


def _parse_log_pairs(raw):
    """로그.txt에서 (입력, 번역) 짝을 뽑는다."""
    pairs, cur = [], None
    for line in raw.splitlines():
        m = re.match(r"\[\d\d:\d\d:\d\d\] 입력: (.*)", line)
        if m:
            cur = m.group(1).strip()
            continue
        m = re.match(r"\[\d\d:\d\d:\d\d\] 번역: (.*)", line)
        if m and cur:
            pairs.append((cur, m.group(1).strip()))
            cur = None
    return pairs


_REVIEW_INSTRUCTION = (
    "아래는 한국어 설교를 실시간 통역한 기록입니다. 각 항목은 음성인식 결과(인식)와 "
    "그것을 영어로 옮긴 것(번역)입니다.\n\n"
    "설교 맥락에서 **음성인식이 잘못 알아들은 곳**을 찾아주세요.\n\n"
    "규칙이 있습니다. 반드시 지켜주세요.\n"
    "1. 원래 무슨 말이었을지 **추측하지 마세요.** 같은 대목이 다른 항목에서 제대로 "
    "인식된 것이 목록 안에 있을 때만 고르세요.\n"
    "2. 그 제대로 인식된 문장을 **글자 그대로** 인용하세요. 요약하거나 고쳐 쓰지 마세요.\n"
    "3. 성경은 앞뒤 절이 비슷해 보여도 서로 다른 문장입니다. 인용한 문장이 정말 "
    "**같은 문장**인지 확인하세요. 다른 절이면 고르지 마세요.\n"
    "4. 고치는 말은 **한국어로** 적으세요(영어 번역이 아니라 원래 한국어 낱말).\n\n"
    "각 건을 이 형식으로 한 줄씩, 다른 설명 없이 쓰세요:\n"
    "잘못 들린 말 | 원래 한국어 말 | 근거 항목번호 | 근거 문장 그대로\n\n"
    "예) 구글 | 죽을 | 41 | 내가 이 불꽃 가운데서 너무 괴로워 죽을 지경입니다\n\n"
    "찾은 것이 없으면 '없음'이라고만 쓰세요.\n\n"
)


def _review_batch(pairs):
    """한 묶음을 검토해 (찾은 것 목록, usage)를 돌려준다.

    클로드의 말을 그대로 믿지 않는다. 인용한 근거 문장이 정말 로그에 있는지,
    그 안에 제안한 낱말이 들어 있는지 검사해서 통과한 것만 '넣기 가능'으로 표시한다.
    """
    body = "\n".join("%d. 인식: %s\n   번역: %s" % (i + 1, k, v)
                     for i, (k, v) in enumerate(pairs))
    msg = client.messages.create(
        model="claude-opus-4-8", max_tokens=2000,
        messages=[{"role": "user", "content": _REVIEW_INSTRUCTION + body}])
    text = msg.content[0].text if msg.content else ""
    inputs = [k for k, _ in pairs]
    joined = "\n".join(inputs)
    out = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4 or not parts[0] or parts[0].startswith("없음"):
            continue
        heard, meant, no, quote = parts[0], parts[1], parts[2], parts[3]
        if heard in _REVIEW_HEADERS or meant in _REVIEW_HEADERS:
            continue
        if re.fullmatch(r"[\d\s.]+", heard) or heard == meant or not meant:
            continue

        # ① 인용한 문장이 정말 로그에 있는가 (지어낸 근거를 걸러낸다)
        quote_ok = bool(quote) and any(quote in s for s in inputs)
        # ② 그 인용 문장 안에 제안한 낱말이 실제로 있는가
        meant_in_quote = bool(quote) and meant in quote
        # ③ 제대로 인식된 적이 아예 있는가 (없으면 추측일 가능성이 높다)
        meant_seen = meant in joined
        verified = quote_ok and meant_in_quote and meant_seen
        # 낱말·짧은 구절만 표에 넣을 수 있다
        fit = verified and len(heard) <= 20

        out.append({"heard": heard, "meant": meant, "line": no,
                    "quote": quote, "verified": verified, "fit": fit})
    return out, msg.usage


@app.route("/review-log", methods=["POST"])
def review_log():
    """예배가 끝난 뒤 그날 기록을 검토한다. 대응표는 바뀌지 않는다."""
    data = request.get_json(silent=True) or {}
    try:
        count = int(data.get("count") or 300)
    except Exception:
        count = 300
    count = max(20, min(_REVIEW_MAX, count))

    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "로그.txt"), "r",
                  encoding="utf-8", errors="ignore") as f:
            raw = f.read()
    except Exception as e:
        return jsonify(error="로그를 읽을 수 없습니다: %s" % str(e)[:80]), 500

    pairs = [p for p in _parse_log_pairs(raw) if re.search(r"[가-힣]", p[0])][-count:]
    if not pairs:
        return jsonify(error="검토할 기록이 없습니다"), 400

    findings, cost = [], 0.0
    try:
        for i in range(0, len(pairs), _REVIEW_BATCH):
            got, usage = _review_batch(pairs[i:i + _REVIEW_BATCH])
            findings += got
            if usage is not None:
                cost += _usage_cost(usage)
    except Exception as e:
        return jsonify(error="검토 중 오류: %s" % str(e)[:120]), 500

    # 같은 낱말이 여러 번 잡히면 하나로 합친다
    seen, merged = set(), []
    for f in sorted(findings, key=lambda x: not x["verified"]):
        key = (f["heard"], f["meant"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(f)

    return jsonify(findings=merged, reviewed=len(pairs), cost=round(cost, 3))

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
    hub.broadcast("__reset__")
    operator_queue.put(("reset", ""))
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


if __name__ == "__main__":
    print("서버 시작")
    print("  운영자 대시보드: http://localhost:5000/operator")
    print("  출력 화면:       http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, threaded=True)
