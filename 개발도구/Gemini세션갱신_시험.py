# -*- coding: utf-8 -*-
"""배포빌드/server.py의 _gemini_live_loop와 Segmenter를 서버 없이 그대로 꺼내 돌린다.
녹음을 브라우저와 같은 85ms 조각으로 실시간 속도로 넣고, 나가는 문장·세션 갱신·오류를
시각과 함께 남긴다. 번역은 하지 않는다(Gemini 음성인식 요금만, 분당 약 $0.009).

사용: python 개발도구/Gemini세션갱신_시험.py <pcm파일> <초> <출력.jsonl>

- 세션 갱신(10분 제한 대응)을 볼 때는 <초>를 600 이상으로. 출력에서 "diag" 줄을 찾으면 된다.
- 빨리 보고 싶으면 환경변수 RENEW_AFTER=60 을 주면 갈아타기를 60초로 당겨 시험한다
  (시험용 사본만 바뀌고 server.py는 그대로).
2026-10-11: 20분 녹음에서 두 번 모두 532초에 쉼에서 갈아탐, 오류 0.
"""
import ast, os, sys, json, time, queue, threading, asyncio, re
from collections import deque
sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv
load_dotenv(os.path.join(os.environ["LOCALAPPDATA"], "Programs", "LiveWord", ".env"), override=True, encoding="utf-8-sig")
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "배포빌드", "server.py"), encoding="utf-8").read()
if os.environ.get("RENEW_AFTER"): src = src.replace("RENEW_AFTER = 530", "RENEW_AFTER = " + os.environ["RENEW_AFTER"])
tree = ast.parse(src)
T0 = time.time()
def stamp(): return round(time.time() - T0, 2)
g = {"re": re, "time": time, "_deque": deque, "threading": threading, "queue": queue, "os": os}
for n in tree.body:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in ("Segmenter", "_gemini_live_loop", "split_sentences") or isinstance(n, ast.FunctionDef):
        try: exec(ast.get_source_segment(src, n), g)
        except Exception: pass
    elif isinstance(n, ast.Assign):
        try: exec(ast.get_source_segment(src, n), g)
        except Exception: pass
exec(ast.get_source_segment(src, [n for n in tree.body if getattr(n, "name", "") == "_gemini_live_loop"][0]), g)
exec(ast.get_source_segment(src, [n for n in tree.body if getattr(n, "name", "") == "Segmenter"][0]), g)
out = open(sys.argv[3], "w", encoding="utf-8")
def rec(**kw):
    kw["t"] = stamp(); out.write(json.dumps(kw, ensure_ascii=False) + "\n"); out.flush()
    if kw.get("kind") != "interim": print(json.dumps(kw, ensure_ascii=False)[:220], flush=True)
g["_log_diag"] = lambda key, msg, min_gap=10.0: rec(kind="diag", msg=msg)
pcm = open(sys.argv[1], "rb").read()[: int(float(sys.argv[2]) * 32000)]
audio_q = queue.Queue(); stop = {"stop": False}; oq = queue.Queue()
def feed():
    C = 2730
    for i in range(0, len(pcm), C):
        audio_q.put(pcm[i:i + C])
        d = T0 + (i + C) / 32000 - time.time()
        if d > 0: time.sleep(d)
    time.sleep(8); stop["stop"] = True; audio_q.put(None)
threading.Thread(target=feed, daemon=True).start()
threading.Thread(target=lambda: asyncio.run(g["_gemini_live_loop"]("ko-KR", audio_q, stop, oq, os.environ["GEMINI_API_KEY"])), daemon=True).start()
S = g["Segmenter"]; seg = S(time.time())
while True:
    item = oq.get()
    if item is None: break
    kind, text = item; now = time.time()
    if kind == "interim": rec(kind="interim", text=text); sents = seg.on_interim(text, now)
    elif kind == "speech_end": rec(kind="speech_end"); sents = seg.on_speech_end(now)
    elif kind == "stream_start": rec(kind="stream_start"); seg = S(now); sents = []
    else: rec(kind=kind, text=text); sents = []
    for s in sents: rec(kind="SENT", text=s)
rec(kind="end")
