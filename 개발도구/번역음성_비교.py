# -*- coding: utf-8 -*-
"""번역 음성 비교 — 지금의 Cloud TTS Standard와 Gemini TTS를 같은 문장으로.

**API를 실제로 부른다. 비용은 1센트 미만이지만 먼저 물어보고 돌릴 것.**

합성 시간을 재고, 들어볼 수 있게 음성 파일을 남긴다.
음성은 tts_jobs에서 **한 번에 하나씩** 만든다(CLAUDE.md 참고). 한 문장 합성이
문장 사이 간격(약 3.5초)보다 오래 걸리면 음성이 자막보다 계속 밀린다.
그래서 음질만이 아니라 '가장 느린 한 번'을 꼭 볼 것.

    python 개발도구/번역음성_비교.py [저장폴더=번역음성_비교]

2026-09-28 결과: Cloud TTS 0.2~0.4초, Gemini 3.8 Flash TTS 2.5~5초(한 번은 76초) → 유지.
관련: 번역모델_비교.py
"""
import os, sys, time, base64
from dotenv import dotenv_values
from google import genai
from google.cloud import texttospeech

APP = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "LiveWord")
OUT = sys.argv[1] if len(sys.argv) > 1 else "번역음성_비교"

# 실제 예배 로그의 번역문 — 짧은 것부터 긴 것까지
SENT = [
    "We will serve it.",
    "Isn't that right?",
    "The steering committee members who know the situation may think that about me behind my back.",
    "No, Pastor is taking all the credit.",
    "I'll treat you to a meal or dinner anytime.",
    "Your officers requested it, so I discussed it with our steering committee members, "
    "and yes, we will provide lunch.",
]


def main():
    # server.py와 같은 순서: APP_DIR\google-key.json이 있으면 그것을 쓴다
    key_json = os.path.join(APP, "google-key.json")
    if os.path.exists(key_json):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = key_json
    gem = genai.Client(api_key=dotenv_values(os.path.join(APP, ".env")).get("GEMINI_API_KEY"))
    cloud = texttospeech.TextToSpeechClient()

    def cloud_tts(t):
        # server.py _tts_and_broadcast()와 같은 설정
        r = cloud.synthesize_speech(
            input=texttospeech.SynthesisInput(text=t),
            voice=texttospeech.VoiceSelectionParams(
                language_code="en-US", name="en-US-Standard-D",
                ssml_gender=texttospeech.SsmlVoiceGender.MALE),
            audio_config=texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3))
        return r.audio_content, "mp3"

    def gem_tts(model, voice):
        def f(t):
            r = gem.interactions.create(
                model=model,
                input=[{"type": "user_input", "content": [{"type": "text", "text": t}]}],
                response_format={"type": "audio"},
                generation_config={"speech_config": [{"voice": voice}]})
            return base64.b64decode(r.output_audio.data), "wav"
        return f

    engines = [
        ("지금_cloud_standard", cloud_tts),
        ("gemini_flash_charon", gem_tts("gemini-3.8-flash-tts", "Charon")),
        ("gemini_lite_charon", gem_tts("gemini-3.8-flash-lite-tts", "Charon")),
    ]
    os.makedirs(OUT, exist_ok=True)

    # 첫 연결 시간이 끼지 않게 한 번씩 데워 둔다
    for _, fn in engines:
        fn("Hello.")

    times = {name: [] for name, _ in engines}
    for i, t in enumerate(SENT):
        for name, fn in engines:
            t0 = time.time()
            try:
                data, ext = fn(t)
                dt = time.time() - t0
                times[name].append(dt)
                open(os.path.join(OUT, f"{i}_{name}.{ext}"), "wb").write(data)
                print(f"  {i} {name:22s} {dt:5.2f}초  {len(data)//1024}KB", flush=True)
            except Exception as e:
                print(f"  {i} {name:22s} 오류: {str(e)[:150]}", flush=True)

    print("\n엔진별 합성 시간")
    for name, ts in times.items():
        if ts:
            print(f"  {name:22s} 가장 빠름 {min(ts):.2f}초 · 가장 느림 {max(ts):.2f}초")
    print(f"\n음성 파일은 {OUT}\\ 에 있습니다.")


if __name__ == "__main__":
    main()
