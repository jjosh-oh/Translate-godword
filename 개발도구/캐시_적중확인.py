# -*- coding: utf-8 -*-
"""현재 배치 vs 제안 배치 — 캐시가 실제로 적중하는지 API로 확인."""
import os, sys
sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv
load_dotenv(os.path.join(os.environ["LOCALAPPDATA"],"Programs","LiveWord",".env"),
            override=True, encoding="utf-8-sig")
import anthropic
from docx import Document

sermon = "\n".join(p.text for p in Document(r"C:\Claude\설교\070526.docx").paragraphs)
reference = ("The sermon script below is a REFERENCE ONLY ...\n\n"
             "--- SERMON REFERENCE (do not output) ---\n" + sermon[:8000] + "\n--- END ---")
base = ("You are a professional church interpreter providing live subtitles. "
        "Translate ONLY the exact given text from Korean into English. Output ONLY the translation.")
def ctx(i):
    return ("\n\nPreceding Korean text (context ONLY):\n문장 %d 앞의 문맥입니다. 매 문장마다 내용이 달라집니다." % i)

c = anthropic.Anthropic()
SENT = ["그 약속은 우리 것입니다.", "하나님은 신실하십니다.", "오늘 이 말씀을 받으십시오.", "함께 기도하겠습니다."]

def run(label, layout):
    print("\n=== %s ===" % label)
    for i, s in enumerate(SENT):
        r = c.messages.create(model="claude-opus-4-8", max_tokens=100,
                              system=layout(i), messages=[{"role":"user","content":s}])
        u = r.usage
        print("  %d회차  캐시기록 %5d · 캐시읽기 %5d · 일반입력 %4d"
              % (i+1, u.cache_creation_input_tokens, u.cache_read_input_tokens, u.input_tokens))

run("현재 배치 — 문맥이 캐시 블록 '앞'에",
    lambda i: [{"type":"text","text":base+ctx(i)},
               {"type":"text","text":reference,"cache_control":{"type":"ephemeral"}}])

run("제안 배치 — 문맥을 캐시 블록 '뒤'로",
    lambda i: [{"type":"text","text":base},
               {"type":"text","text":reference,"cache_control":{"type":"ephemeral"}},
               {"type":"text","text":ctx(i)}])
