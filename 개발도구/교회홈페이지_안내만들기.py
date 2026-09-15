# 교인용 통역 안내 자료를 한 번에 만든다.
#
#   python 개발도구/교회홈페이지_안내만들기.py
#
# 만들어지는 것 (모두 교회홈페이지\ 폴더):
#   통역안내.html      ← 워드프레스에 통째로 붙여넣을 페이지. 포스터 그림과 QR이 파일 안에 들어 있다.
#   통역안내.pdf       ← 같은 안내를 letter 세로 2쪽으로 (나눠 주는 용)
#   안내데스크.pdf     ← 안내데스크에 세워 두는 한 장 (letter 가로)
#   통역포스터.pdf     ← 예배당 게시용. 배포빌드\poster.html 을 그대로 그려서 만든다 (letter 가로)
#   통역포스터.jpg     ← 같은 포스터의 그림 파일
#   주보안내문.png     ← 주보 문서에 끼워 넣는 작은 안내 상자 (100×53mm, 300dpi)
#   주보안내문.pdf     ← 같은 것의 인쇄용
#
# 인쇄물은 모두 letter(8.5×11in) — 미국 교회라 A4를 쓰지 않는다. 종이 크기는 각 HTML의 @page 가 정한다.
# 다섯 가지 모두에 음성(이어폰) 안내가 들어간다. 포스터의 음성 안내는 poster.html 의 4단계인데,
# 교회 컴퓨터에 설치된 poster.html 이 옛것이면 그 단계가 없다. 여기서는 저장소의 최신 파일을 쓴다.
#
# 주소가 두 가지인 것에 주의할 것.
#   MOBILE_URL — 자막 화면으로 바로 가는 주소(ngrok). 포스터와 홈페이지 단추가 쓴다.
#                교회 컴퓨터에 설정된 것이 진짜다. 개발 PC의 .env를 믿지 말 것.
#   PAGE_URL   — 교회 홈페이지 안내 페이지. 주보와 안내데스크 QR은 이쪽을 가리킨다.
#                인쇄물은 한 번 나가면 못 고친다. ngrok 주소가 바뀌어도 홈페이지만
#                고치면 되도록 한 단계 거쳐 가게 했다.

import base64
import io
import os
import subprocess

import pymupdf  # pip install pymupdf
import qrcode  # pip install qrcode pillow
from PIL import Image

MOBILE_URL = "https://paralegal-slackness-voter.ngrok-free.dev/m"
PAGE_URL = "https://saeroun.org/translate"

CHURCH_NAME = "Saeroun Reformed Church"
CHURCH_WEB = "www.saeroun.org"

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(HERE, "교회홈페이지")
POSTER_HTML = os.path.join(HERE, "배포빌드", "poster.html")
TEMPLATE = os.path.join(OUT_DIR, "_template.html")
BULLETIN = os.path.join(OUT_DIR, "_주보안내문.html")
DESK = os.path.join(OUT_DIR, "_안내데스크.html")
LOGO = os.path.join(OUT_DIR, "_로고.png")


# ── 재료 ────────────────────────────────────────────────────────────────────

def file_data_uri(path: str, mime: str) -> str:
    with open(path, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


def qr_data_uri(url: str, color: str = "#1b2a63") -> str:
    """QR을 PNG로 만들어 data: 주소로 돌려준다. 외부 QR 서비스에 기대지 않는다."""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=12, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color=color, back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def chrome_pdf(html: str, out_pdf: str, tmp_name: str) -> None:
    """HTML 한 덩이를 크롬으로 그려 PDF로 뽑는다. 종이 크기는 각 HTML의 @page 가 정한다."""
    tmp = os.path.join(OUT_DIR, tmp_name)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         "--no-pdf-header-footer", f"--print-to-pdf={out_pdf}",
         "file:///" + tmp.replace("\\", "/")],
        check=True, capture_output=True,
    )
    os.remove(tmp)


# ── 만들기 ──────────────────────────────────────────────────────────────────

def build_poster() -> Image.Image:
    """배포빌드\\poster.html 을 그대로 그려 포스터를 만들고, 그 그림을 돌려준다.

    화면 없이 그리므로 /settings·/tunnel-url 요청은 실패한다. 로고·교회 이름·주소·QR을
    아래 스크립트로 직접 넣어 준다. (제목과 언어 알약은 HTML 기본값이 이미 맞다)
    """
    with open(POSTER_HTML, "r", encoding="utf-8") as f:
        html = f.read()

    inject = """
<script>
window.addEventListener('load', function(){
  applyLogo(%r); applyChurch(%r); applyWeb(%r);
  var img = document.getElementById('qr-img');
  img.src = %r; img.style.display = 'block';
  document.getElementById('qr-ph').style.display = 'none';
  document.getElementById('qr-url').textContent = %r;
});
</script>
</body>""" % (file_data_uri(LOGO, "image/png"), CHURCH_NAME, CHURCH_WEB,
              qr_data_uri(MOBILE_URL, "black"), MOBILE_URL)

    pdf = os.path.join(OUT_DIR, "통역포스터.pdf")
    chrome_pdf(html.replace("</body>", inject, 1), pdf, "_포스터_임시.html")

    doc = pymupdf.open(pdf)
    img = Image.open(io.BytesIO(doc[0].get_pixmap(dpi=300).tobytes("png")))
    img.convert("RGB").save(os.path.join(OUT_DIR, "통역포스터.jpg"), quality=88, optimize=True)
    return img


def poster_data_uri(img: Image.Image, width: int = 1600) -> str:
    """홈페이지에 박아 넣을 포스터 그림.

    색이 단순해서 PNG로 줄여도 깨끗하지만, 64색까지 내리면 교회 로고의 노란색이
    회색으로 죽는다. 192색이면 제대로 나오고 25KB만 더 든다.
    """
    small = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    small = small.convert("RGB").quantize(colors=192, method=Image.MEDIANCUT, dither=Image.NONE)
    buf = io.BytesIO()
    small.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def build_page(poster: Image.Image) -> None:
    with open(TEMPLATE, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__POSTER__", poster_data_uri(poster))
    html = html.replace("__QR__", qr_data_uri(MOBILE_URL))
    html = html.replace("__URL__", MOBILE_URL)
    with open(os.path.join(OUT_DIR, "통역안내.html"), "w", encoding="utf-8") as f:
        f.write(html)


def build_page_pdf() -> None:
    """홈페이지 안내를 그대로 letter 인쇄용 PDF로. 인쇄 규칙은 _template.html 의 @media print."""
    src = "file:///" + os.path.join(OUT_DIR, "통역안내.html").replace("\\", "/")
    subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         "--no-pdf-header-footer", f"--print-to-pdf={os.path.join(OUT_DIR, '통역안내.pdf')}", src],
        check=True, capture_output=True,
    )


def build_bulletin() -> None:
    """주보용 안내 상자 — 그림(PNG)과 인쇄용(PDF) 두 가지."""
    with open(BULLETIN, "r", encoding="utf-8") as f:
        html = f.read().replace("__QR_PAGE__", qr_data_uri(PAGE_URL))

    tmp = os.path.join(OUT_DIR, "_주보안내문_임시.html")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    src = "file:///" + tmp.replace("\\", "/")
    common = ["--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars"]

    # 100mm × 53mm = CSS 378 × 200px. 300dpi로 뽑으려면 3.13배.
    subprocess.run(
        [CHROME, *common, f"--screenshot={os.path.join(OUT_DIR, '주보안내문.png')}",
         "--window-size=378,200", "--force-device-scale-factor=3.13", src],
        check=True, capture_output=True,
    )
    subprocess.run(
        [CHROME, *common, "--no-pdf-header-footer",
         f"--print-to-pdf={os.path.join(OUT_DIR, '주보안내문.pdf')}", src],
        check=True, capture_output=True,
    )
    os.remove(tmp)


def build_desk() -> None:
    """안내데스크에 비치할 letter 가로 한 장."""
    with open(DESK, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__QR_PAGE__", qr_data_uri(PAGE_URL))
    html = html.replace("__LOGO__", file_data_uri(LOGO, "image/png"))
    chrome_pdf(html, os.path.join(OUT_DIR, "안내데스크.pdf"), "_안내데스크_임시.html")


def main() -> None:
    poster = build_poster()
    build_page(poster)
    build_page_pdf()
    build_desk()
    build_bulletin()

    for name in ("통역안내.html", "통역안내.pdf", "안내데스크.pdf",
                 "통역포스터.pdf", "통역포스터.jpg", "주보안내문.png", "주보안내문.pdf"):
        p = os.path.join(OUT_DIR, name)
        print(f"  {name:<16} {os.path.getsize(p):>9,} 바이트")
    print(f"\n  자막 화면 주소 : {MOBILE_URL}")
    print(f"  인쇄물 QR 주소 : {PAGE_URL}")


if __name__ == "__main__":
    main()
