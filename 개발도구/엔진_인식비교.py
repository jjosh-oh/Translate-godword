# -*- coding: utf-8 -*-
"""같은 설교 음성을 V1(latest_long)과 chirp_3에 각각 흘려 넣어
인식 결과를 나란히 비교한다. 번역은 하지 않으므로 Claude 비용은 0.

사용: python engine_compare.py <pcm파일> <언어코드>
"""
import os
import sys
import json
import time
import queue
import threading

sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv

load_dotenv(os.path.join(os.environ["LOCALAPPDATA"], "Programs", "LiveWord", ".env"),
            override=True, encoding="utf-8-sig")

PCM_PATH = sys.argv[1]
LANG = sys.argv[2] if len(sys.argv) > 2 else "ko-KR"
CHUNK = 3200            # 100ms
pcm = open(PCM_PATH, "rb").read()
DURATION = len(pcm) / 32000.0


def project_id():
    for k in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_QUOTA_PROJECT"):
        if os.environ.get(k):
            return os.environ[k]
    try:
        d = json.load(open(os.environ["GOOGLE_APPLICATION_CREDENTIALS"], encoding="utf-8"))
        return d.get("project_id") or d.get("quota_project_id")
    except Exception:
        pass
    import google.auth
    return google.auth.default()[1]


def feeder(q):
    """실제 예배처럼 실시간 속도로 흘려 넣는다."""
    for i in range(0, len(pcm), CHUNK):
        q.put(pcm[i:i + CHUNK])
        time.sleep(0.1)
    q.put(None)


def run_v1():
    from google.cloud import speech
    q = queue.Queue()
    threading.Thread(target=feeder, args=(q,), daemon=True).start()
    cfg = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=16000, language_code=LANG,
        enable_automatic_punctuation=True, model="latest_long", use_enhanced=True)
    scfg = speech.StreamingRecognitionConfig(
        config=cfg, interim_results=True, enable_voice_activity_events=True)

    def gen():
        while True:
            c = q.get()
            if c is None:
                return
            yield speech.StreamingRecognizeRequest(audio_content=c)

    out, t0, first = [], time.time(), None
    for resp in speech.SpeechClient().streaming_recognize(scfg, gen()):
        if not resp.results:
            continue
        r = resp.results[0]
        if first is None and r.alternatives and r.alternatives[0].transcript.strip():
            first = time.time() - t0
        if r.is_final and r.alternatives:
            tr = r.alternatives[0].transcript.strip()
            if tr:
                out.append((time.time() - t0, tr))
    return out, first


def run_chirp3():
    from google.api_core.client_options import ClientOptions
    from google.cloud.speech_v2 import SpeechClient
    from google.cloud.speech_v2.types import cloud_speech as t
    q = queue.Queue()
    threading.Thread(target=feeder, args=(q,), daemon=True).start()

    client = SpeechClient(client_options=ClientOptions(
        api_endpoint="us-speech.googleapis.com"))
    recognizer = "projects/%s/locations/us/recognizers/_" % project_id()
    config = t.RecognitionConfig(
        explicit_decoding_config=t.ExplicitDecodingConfig(
            encoding=t.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000, audio_channel_count=1),
        language_codes=[LANG], model="chirp_3",
        features=t.RecognitionFeatures(enable_automatic_punctuation=True),
        denoiser_config=t.DenoiserConfig(denoise_audio=True))
    scfg = t.StreamingRecognitionConfig(
        config=config,
        streaming_features=t.StreamingRecognitionFeatures(
            interim_results=True, enable_voice_activity_events=True))

    def gen():
        yield t.StreamingRecognizeRequest(recognizer=recognizer, streaming_config=scfg)
        while True:
            c = q.get()
            if c is None:
                return
            yield t.StreamingRecognizeRequest(audio=c)

    out, t0, first = [], time.time(), None
    for resp in client.streaming_recognize(requests=gen()):
        if not resp.results:
            continue
        r = resp.results[0]
        if first is None and r.alternatives and r.alternatives[0].transcript.strip():
            first = time.time() - t0
        if r.is_final and r.alternatives:
            tr = r.alternatives[0].transcript.strip()
            if tr:
                out.append((time.time() - t0, tr))
    return out, first


def show(name, res):
    out, first = res
    text = " ".join(s for _, s in out)
    print("\n" + "=" * 74)
    print("[%s]  최종 문장 %d개 · 첫 인식 %.1f초 · 총 %d자"
          % (name, len(out), first or 0, len(text)))
    print("=" * 74)
    for t, s in out:
        print("  %5.1fs  %s" % (t, s))
    return text


print("설교 음성 %.0f초 · 언어 %s" % (DURATION, LANG))
print("두 엔진에 같은 음성을 실시간 속도로 흘려 넣습니다. 각 %.0f초씩 걸립니다." % DURATION)

a = run_v1()
b = run_chirp3()
ta = show("V1 · latest_long + enhanced (지금 쓰는 것)", a)
tb = show("chirp_3 · V2 + 잡음제거", b)

print("\n" + "=" * 74)
print("요약")
print("=" * 74)
print("  %-34s %-16s %s" % ("", "V1", "chirp_3"))
print("  %-34s %-16d %d" % ("최종 문장 수", len(a[0]), len(b[0])))
print("  %-34s %-16.1f %.1f" % ("첫 인식까지(초)", a[1] or 0, b[1] or 0))
print("  %-34s %-16d %d" % ("인식된 글자 수", len(ta), len(tb)))
print("  %-34s %-16d %d" % ("마침표 개수", ta.count(".") + ta.count("?") + ta.count("!"),
                            tb.count(".") + tb.count("?") + tb.count("!")))
print("  %-34s %-16d %d" % ("쉼표 개수", ta.count(","), tb.count(",")))
