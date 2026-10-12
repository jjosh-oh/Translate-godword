# -*- coding: utf-8 -*-
"""Gemini Live에 녹음을 실시간 속도로 흘려 넣고 서버가 보내는 것을 전부 시각과 함께 남긴다.
번역은 하지 않는다 — Gemini 음성인식 요금만 든다(분당 약 $0.009).

사용: python 개발도구/Gemini_세션관찰.py <pcm파일> <초> <출력.jsonl> [맞춤어휘.txt]

- 세션 10분 제한을 볼 때: <초>를 600 이상으로. 2026-10-11에 9분 GoAway(50초 남음),
  9분 50초 강제 종료(1008)를 확인했다.
- 맞춤 어휘(adaptation_phrases)를 시험할 때: 한 줄에 하나씩 적은 파일을 넷째 인자로.
  결과 텍스트는 같은 폴더의 Gemini_세션관찰_분석.py로 모아 볼 수 있다.
프로그램과 같은 설정(ko-KR 고정, 실시간 interim)을 쓴다. LiveWord가 꺼져 있어도 된다.
"""
import os, sys, time, json, asyncio
sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv
load_dotenv(os.path.join(os.environ["LOCALAPPDATA"], "Programs", "LiveWord", ".env"),
            override=True, encoding="utf-8-sig")
from google import genai
from google.genai import types

PCM, SECS, OUT = sys.argv[1], float(sys.argv[2]), sys.argv[3]
PHRASES = None
if len(sys.argv) > 4:
    PHRASES = [l.strip() for l in open(sys.argv[4], encoding="utf-8") if l.strip()]
pcm = open(PCM, "rb").read()[: int(SECS * 32000)]
CHUNK = 3200
out = open(OUT, "w", encoding="utf-8")
T0 = None

def rec(kind, **kw):
    t = time.time() - T0
    kw.update(t=round(t, 2), kind=kind)
    out.write(json.dumps(kw, ensure_ascii=False) + "\n"); out.flush()
    if kind not in ("interim",):
        print(f"[{t:7.2f}] {kind} {json.dumps({k:v for k,v in kw.items() if k not in ('t','kind')}, ensure_ascii=False)[:200]}", flush=True)

async def main():
    global T0
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    atc = dict(language_hints=types.LanguageHints(language_codes=["ko-KR"]))
    if PHRASES:
        atc["adaptation_phrases"] = PHRASES
    config = types.LiveConnectConfig(
        response_modalities=["TEXT"],
        input_audio_transcription=types.AudioTranscriptionConfig(**atc))
    T0 = time.time()
    sent = {"sec": 0.0}
    try:
        async with client.aio.live.connect(model="gemini-3.5-transcribe-live", config=config) as session:
            rec("connected")
            async def sender():
                for i in range(0, len(pcm), CHUNK):
                    await session.send_realtime_input(
                        audio=types.Blob(data=pcm[i:i+CHUNK], mime_type="audio/pcm;rate=16000"))
                    sent["sec"] = (i + CHUNK) / 32000
                    target = T0 + sent["sec"]
                    d = target - time.time()
                    if d > 0:
                        await asyncio.sleep(d)
                rec("audio_done", audio_sec=sent["sec"])
                await asyncio.sleep(8)
                await session.send_realtime_input(audio_stream_end=True)
                await asyncio.sleep(5)
            async def receiver():
                async for r in session.receive():
                    if getattr(r, "go_away", None):
                        rec("go_away", time_left=str(r.go_away.time_left), audio_sec=sent["sec"])
                    if getattr(r, "session_resumption_update", None):
                        rec("resumption_update", v=str(r.session_resumption_update)[:150])
                    va = getattr(r, "voice_activity", None)
                    if va:
                        rec("voice_activity", v=str(va.voice_activity_type))
                    sc = r.server_content
                    if not sc:
                        continue
                    it = getattr(sc, "interim_input_transcription", None)
                    if it and it.text:
                        rec("interim", text=it.text, audio_sec=sent["sec"])
                    fi = getattr(sc, "input_transcription", None)
                    if fi and fi.text:
                        rec("final", n=len(fi.text), text=fi.text, audio_sec=sent["sec"])
                    if getattr(sc, "turn_complete", None):
                        rec("turn_complete", audio_sec=sent["sec"])
                rec("receive_ended", audio_sec=sent["sec"])
            st = asyncio.create_task(sender()); rt = asyncio.create_task(receiver())
            done, pending = await asyncio.wait({st, rt}, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                rec("task_done", which="sender" if t is st else "receiver",
                    exc=repr(t.exception())[:300] if t.exception() else None, audio_sec=sent["sec"])
            for t in pending: t.cancel()
    except Exception as e:
        rec("exception", exc=repr(e)[:400], audio_sec=sent["sec"])
    rec("end", audio_sec=sent["sec"])

asyncio.run(main())
