# -*- coding: utf-8 -*-
"""chirp_3가 중간 결과(interim)를 얼마나 자주 주는지 실측한다.
자막이 나가는 속도는 결국 중간 결과가 얼마나 자주 오느냐로 정해진다."""
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

PCM = open(sys.argv[1], "rb").read()
ENGINE = sys.argv[2]
LANG = "ko-KR"


def project_id():
    d = json.load(open(os.environ["GOOGLE_APPLICATION_CREDENTIALS"], encoding="utf-8"))
    return d.get("project_id") or d.get("quota_project_id")


q = queue.Queue()


def feeder():
    for i in range(0, len(PCM), 3200):
        q.put(PCM[i:i + 3200])
        time.sleep(0.1)
    q.put(None)


threading.Thread(target=feeder, daemon=True).start()

if ENGINE == "v1":
    from google.cloud import speech
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
    stream = speech.SpeechClient().streaming_recognize(scfg, gen())
else:
    from google.api_core.client_options import ClientOptions
    from google.cloud.speech_v2 import SpeechClient
    from google.cloud.speech_v2.types import cloud_speech as t
    client = SpeechClient(client_options=ClientOptions(
        api_endpoint="us-speech.googleapis.com"))
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
        yield t.StreamingRecognizeRequest(
            recognizer="projects/%s/locations/us/recognizers/_" % project_id(),
            streaming_config=scfg)
        while True:
            c = q.get()
            if c is None:
                return
            yield t.StreamingRecognizeRequest(audio=c)
    stream = client.streaming_recognize(requests=gen())

t0 = time.time()
n_int = n_fin = 0
gaps = []
last = 0.0
prev_len = 0
grow_events = []
for resp in stream:
    if not resp.results:
        continue
    r = resp.results[0]
    if not r.alternatives:
        continue
    tr = r.alternatives[0].transcript
    now = time.time() - t0
    if r.is_final:
        n_fin += 1
        gaps.append(now - last)
        last = now
        prev_len = 0
    else:
        n_int += 1
        if len(tr) > prev_len:
            grow_events.append((now, len(tr)))
            prev_len = len(tr)

print("=== %s ===" % ENGINE)
print("  중간 결과(interim) 응답 수 : %d" % n_int)
print("  최종 결과(final)   응답 수 : %d" % n_fin)
print("  중간 결과가 길어진 횟수    : %d" % len(grow_events))
if n_int:
    print("  중간 결과 평균 간격        : %.2f초" % (120.0 / n_int))
else:
    print("  ⚠ 중간 결과가 하나도 오지 않았습니다")
if gaps:
    print("  최종 결과 사이 최대 간격   : %.1f초" % max(gaps))
