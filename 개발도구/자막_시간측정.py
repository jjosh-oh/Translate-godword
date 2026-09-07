# -*- coding: utf-8 -*-
"""같은 설교 음성을 실제 프로그램(/audio)에 흘려 넣어, 엔진별로
송출 화면에 자막이 언제 몇 개 나오는지 잰다.

사용: python server_compare.py <pcm파일> <원어코드> <대표언어> <엔진>
"""
import os
import sys
import json
import time
import threading

sys.stdout.reconfigure(encoding="utf-8")
import requests
import websocket

PCM, SRC, TGT, ENGINE = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
BASE = "http://127.0.0.1:5000"
pcm = open(PCM, "rb").read()

requests.post(BASE + "/settings", json={"target_lang": TGT, "stt_engine": ENGINE}, timeout=5)
requests.post(BASE + "/clear-display", timeout=5)
time.sleep(0.5)

t0 = time.time()
subs = []


def watch():
    r = requests.get(BASE + "/stream", stream=True, timeout=400)
    cur = []
    for raw in r.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data: "):
            continue
        d = raw[6:]
        if d == "__clear__":
            cur = []
        elif d == "__done__":
            if cur:
                subs.append((time.time() - t0, "".join(cur)))
            cur = []
        elif d.startswith("__settings__") or d == "__reset__":
            pass
        else:
            cur.append(d)


threading.Thread(target=watch, daemon=True).start()
time.sleep(1)

ws = websocket.create_connection("ws://127.0.0.1:5000/audio", timeout=30)
ws.send(json.dumps({"source_lang_code": SRC, "target_lang": TGT}))
for i in range(0, len(pcm), 3200):
    ws.send_binary(pcm[i:i + 3200])
    time.sleep(0.1)
time.sleep(25)          # 남은 번역 대기
try:
    ws.close()
except Exception:
    pass
time.sleep(3)

print("\n===== 엔진 %s · 자막 %d건 =====" % (ENGINE, len(subs)))
prev, worst = 0.0, 0.0
for t, s in subs:
    worst = max(worst, t - prev)
    prev = t
    print("  %5.1fs  %s" % (t, s))
print("  최대 공백 %.1f초 · 평균 간격 %.1f초"
      % (worst, (subs[-1][0] / len(subs)) if subs else 0))
