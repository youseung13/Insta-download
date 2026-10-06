#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
공개 게시물 전용 모바일 다운로드 웹앱
------------------------------------------------
- 인스타그램, 유튜브 등 yt-dlp가 지원하는 사이트의 "공개"(로그인 없이 누구나 볼 수 있는)
  게시물/영상만 대상으로 합니다.
- 다른 사람의 비공개 계정/스토리에 접근하는 기능은 넣지 않았습니다.
  (스토리·비공개 콘텐츠는 인스타그램이 의도적으로 로그인 뒤에 막아둔 것이라
   "아이디만 넣으면 받아진다" 식의 도구는 만들지 않습니다.)
- IG_COOKIES_TXT 환경변수에 "본인" 인스타그램 계정의 로그인 쿠키(cookies.txt,
  Netscape 형식)를 넣어두면, 요청이 로그인된 상태로 나가서 인스타그램의
  비로그인 요청 차단(429 Too Many Requests)을 덜 받습니다. 이건 다른 계정에
  접근하기 위한 게 아니라 "내 세션으로 공개 게시물을 조회"하는 용도이고,
  설정하지 않아도 동작은 합니다(다만 429가 더 자주 날 수 있음).
  (주의: 이 쿠키는 보통 몇 주~몇 달 뒤 또는 인스타그램 비밀번호 변경/전체 로그아웃 시
   만료됩니다. 그러면 429가 다시 잦아질 수 있으니 그때는 쿠키를 새로 추출해서
   IG_COOKIES_TXT를 갱신하면 됩니다.)
- 폰 브라우저로 어디서든 접속해서 쓰는 용도이므로, Render.com 같은 호스팅에
  올려서 공개 URL을 받는 걸 전제로 만들었습니다. (배포 방법은 README.md 참고)
- PWA(홈 화면에 추가 시 앱처럼 전체화면으로 열림)를 지원합니다. static/ 폴더의
  manifest.json, 아이콘, 서비스워커가 필요합니다.
- 여러 장(캐러셀) 게시물은 모든 항목을 받아서 zip 파일로 묶어줍니다.
- 영상 대신 mp3(음성만)로 받는 옵션을 제공합니다.
- 다운로드 전에 제목/썸네일을 먼저 확인할 수 있는 미리보기 기능이 있습니다.
- /healthz 는 인증 없이 응답하는 매우 가벼운 상태확인용 경로입니다. UptimeRobot 같은
  외부 핑 서비스로 주기적으로 호출하면 Render 무료 서버가 잠들지 않게 할 수 있습니다.

로컬에서 테스트:
    pip install -r requirements.txt
    python app.py
    -> http://localhost:8000 접속 (같은 와이파이의 폰에서는 http://PC의사설IP:8000)

주의:
    - 본인 소유 콘텐츠 백업, 또는 저작권/초상권 문제가 없는 용도로만 사용하세요.
    - 다른 사람의 콘텐츠를 재배포하는 용도로 쓰지 마세요.
"""
import hashlib
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile

from flask import Flask, jsonify, request, send_file, session, redirect, url_for

import yt_dlp

app = Flask(__name__)

# Render 등에 배포할 때 환경변수 APP_PASSWORD를 설정하면
# 아무나 내 공개 URL로 들어와서 남의 서버 자원을 쓰는 걸 막을 수 있습니다.
# (설정하지 않으면 비밀번호 없이 누구나 접속 가능하니, 외부에 공개하는 서버라면 꼭 설정하세요.)
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
DOWNLOAD_TIMEOUT = 60
PREVIEW_TIMEOUT = 20

# 세션 서명용 비밀키. 매번 무작위로 만들면(uuid4 등) gunicorn이 워커를
# 여러 개 띄울 때 워커마다 키가 달라져서, 로그인 직후 다른 워커가 요청을
# 받으면 세션이 깨져 "비밀번호 틀림"처럼 보이는 버그가 생긴다.
# APP_PASSWORD(모든 워커가 동일하게 갖는 값) 기반으로 고정된 키를 만들어
# 이 문제를 막는다. 별도의 SECRET_KEY 환경변수를 설정했다면 그걸 우선 사용.
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(
    f"insta-web-downloader::{APP_PASSWORD}".encode("utf-8")
).hexdigest()


def _prepare_cookie_file():
    """IG_COOKIES_TXT 환경변수(Netscape 형식 cookies.txt 전체 내용)가 있으면
    임시 파일로 저장해서 그 경로를 돌려준다. 없거나 쓰기 실패하면 None."""
    raw = os.environ.get("IG_COOKIES_TXT", "").strip()
    if not raw:
        return None
    try:
        fd, path = tempfile.mkstemp(prefix="ig_cookies_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if not raw.startswith("#"):
                # Netscape 쿠키 파일 헤더가 없으면 yt-dlp가 형식을 못 알아볼 수 있어 보정
                f.write("# Netscape HTTP Cookie File\n")
            f.write(raw + "\n")
        return path
    except Exception:
        return None


# 서버 프로세스(워커)당 한 번만 파일로 써둔다.
COOKIE_FILE = _prepare_cookie_file()

# 캐러셀(여러 장) 게시물을 전체 다 받을지는 사이트별로 다르게 판단한다.
# - 인스타그램: "캐러셀 게시물의 모든 항목"을 받는 게 목적이므로 playlist 확장을 허용.
# - 그 외(유튜브 등): 사용자가 재생목록 URL을 실수로 붙여넣었을 때 전체 재생목록을
#   통째로 받아버리는 사고를 막기 위해, 링크가 가리키는 "그 영상 하나"만 받는다.
def _noplaylist_for(url: str) -> bool:
    return "instagram.com" not in url.lower()


PWA_HEAD = """
<link rel="manifest" href="/static/manifest.json">
<meta name="theme-color" content="#2563eb">
<link rel="apple-touch-icon" href="/static/icon-192.png">
<link rel="icon" href="/static/icon-192.png">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<script>
if ('serviceWorker' in navigator) {
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/static/sw.js').catch(function () {});
  });
}
</script>
"""

PAGE_STYLE = """
<style>
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 480px; margin: 40px auto; padding: 0 16px; color:#222; }
  h1 { font-size: 20px; }
  input[type=text], input[type=password], select {
    width: 100%; padding: 12px; font-size: 16px; box-sizing: border-box;
    border:1px solid #ccc; border-radius: 8px; margin-bottom: 12px;
  }
  button { width: 100%; padding: 14px; font-size: 16px; border: none; border-radius: 8px; background: #2563eb; color: white; margin-bottom: 8px; }
  button.secondary { background: #e5e7eb; color: #222; }
  button:disabled { background: #999; }
  .note { font-size: 13px; color: #777; margin-top: 20px; line-height: 1.6; }
  .error { color: #c0392b; margin-top: 12px; white-space: pre-wrap; font-size: 14px; }
  #previewArea { display:none; margin: 4px 0 16px; padding: 10px; border:1px solid #eee; border-radius: 8px; }
  #previewArea img { max-width: 100%; border-radius: 6px; display:block; }
  #previewTitle { margin-top: 8px; font-size: 14px; color: #333; line-height:1.4; }
  #previewError { display:none; }
</style>
"""

LOGIN_HTML = """
<!doctype html><html><head><meta name=viewport content="width=device-width, initial-scale=1">
<title>로그인</title>{style}{pwa}</head><body>
<h1>비밀번호 입력</h1>
<form method=post>
<input type=password name=pw placeholder="비밀번호" autofocus>
<button type=submit>입장</button>
</form>
{error}
</body></html>
"""

INDEX_HTML = """
<!doctype html><html><head><meta name=viewport content="width=device-width, initial-scale=1">
<title>공개 게시물 다운로드</title>{style}{pwa}</head><body>
<h1>공개 게시물 다운로드</h1>
<form method=post action="/download" id=f>
<input type=text name=url id=urlInput placeholder="인스타그램/유튜브 등 공개 게시물 URL" autofocus>

<button type=button id=previewBtn class=secondary>미리보기</button>
<div id=previewArea>
  <img id=previewImg style="display:none">
  <div id=previewTitle></div>
</div>
<div class="error" id=previewError></div>

<select name=fmt id=fmtSelect>
  <option value="video" selected>영상 (최고화질)</option>
  <option value="mp3">MP3 (음성만 추출)</option>
</select>

<button type=submit id=btn>다운로드</button>
</form>
<script>
document.getElementById('f').addEventListener('submit', function(){{
  var b = document.getElementById('btn');
  b.disabled = true;
  b.textContent = '다운로드 중... (몇 초~수십 초 걸릴 수 있어요)';
}});

document.getElementById('previewBtn').addEventListener('click', async function(){{
  var btn = this;
  var url = document.getElementById('urlInput').value.trim();
  var area = document.getElementById('previewArea');
  var err = document.getElementById('previewError');
  area.style.display = 'none';
  err.style.display = 'none';
  if (!url) {{
    err.textContent = 'URL을 먼저 입력해 주세요.';
    err.style.display = 'block';
    return;
  }}
  btn.disabled = true;
  var original = btn.textContent;
  btn.textContent = '불러오는 중...';
  try {{
    var res = await fetch('/preview', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{url: url}})
    }});
    var data = await res.json();
    if (!res.ok || data.error) {{
      err.textContent = data.error || '미리보기를 가져오지 못했습니다.';
      err.style.display = 'block';
    }} else {{
      var titleEl = document.getElementById('previewTitle');
      titleEl.textContent = data.title + (data.count > 1 ? ' (총 ' + data.count + '개 항목)' : '');
      var img = document.getElementById('previewImg');
      if (data.thumbnail) {{
        img.src = data.thumbnail;
        img.style.display = 'block';
      }} else {{
        img.style.display = 'none';
      }}
      area.style.display = 'block';
    }}
  }} catch (e) {{
    err.textContent = '미리보기 중 오류가 발생했습니다.';
    err.style.display = 'block';
  }}
  btn.disabled = false;
  btn.textContent = original;
}});
</script>
{error}
<div class=note>
공개(로그인 없이 누구나 볼 수 있는) 게시물/영상만 지원합니다.<br>
비공개 계정, 스토리, 로그인이 필요한 콘텐츠는 지원하지 않습니다.<br>
여러 장(캐러셀) 게시물은 전체 항목을 받아 zip 파일로 묶어 드립니다.<br>
본인 소유 콘텐츠 백업, 또는 저작권/초상권 문제가 없는 용도로만 사용하세요.
</div>
</body></html>
"""


def check_auth():
    if not APP_PASSWORD:
        return True
    return session.get("authed") is True


@app.route("/", methods=["GET"])
def index():
    if not check_auth():
        return redirect(url_for("login"))
    return INDEX_HTML.format(style=PAGE_STYLE, pwa=PWA_HEAD, error="")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        return redirect(url_for("index"))
    error = ""
    if request.method == "POST":
        if request.form.get("pw") == APP_PASSWORD:
            session["authed"] = True
            return redirect(url_for("index"))
        error = "<div class=error>비밀번호가 틀렸습니다.</div>"
    return LOGIN_HTML.format(style=PAGE_STYLE, pwa=PWA_HEAD, error=error)


@app.route("/healthz", methods=["GET"])
def healthz():
    # 인증 없이 응답하는 아주 가벼운 상태확인 경로.
    # UptimeRobot 등 외부 핑 서비스가 주기적으로 호출하면
    # Render 무료 서버가 15분 무활동으로 잠드는 걸 막을 수 있다.
    return "ok", 200


def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|\n\r\t]', "_", name).strip()
    return name[:150] or "download"


def _schedule_cleanup(path: str, delay: float = 30.0):
    def _cleanup():
        time.sleep(delay)
        shutil.rmtree(path, ignore_errors=True)

    threading.Thread(target=_cleanup, daemon=True).start()


@app.route("/preview", methods=["POST"])
def preview():
    if not check_auth():
        return jsonify({"error": "로그인이 필요합니다."}), 401

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "올바른 URL을 입력해 주세요."}), 400

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": _noplaylist_for(url),
        "socket_timeout": PREVIEW_TIMEOUT,
        "extract_flat": "in_playlist",
    }
    if COOKIE_FILE:
        ydl_opts["cookiefile"] = COOKIE_FILE

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        msg = str(e)
        if "429" in msg:
            msg = "요청이 너무 많아 일시적으로 막혔습니다. 잠시 후 다시 시도해 주세요."
        elif any(k in msg.lower() for k in ("login", "private", "authentication")):
            msg = "비공개 계정/스토리이거나 로그인이 필요한 콘텐츠일 수 있습니다."
        return jsonify({"error": msg}), 400

    if not info:
        return jsonify({"error": "정보를 가져오지 못했습니다."}), 400

    entries = info.get("entries")
    if entries:
        entries = list(entries)
        first = entries[0] if entries else info
        count = len(entries)
    else:
        first = info
        count = 1

    title = (first or {}).get("title") or info.get("title") or "(제목 없음)"
    thumbnail = (first or {}).get("thumbnail") or info.get("thumbnail") or ""

    return jsonify({"title": title, "thumbnail": thumbnail, "count": count})


@app.route("/download", methods=["POST"])
def download():
    if not check_auth():
        return redirect(url_for("login"))

    url = (request.form.get("url") or "").strip()
    fmt = (request.form.get("fmt") or "video").strip()
    if not url.startswith(("http://", "https://")):
        return INDEX_HTML.format(
            style=PAGE_STYLE, pwa=PWA_HEAD, error="<div class=error>올바른 URL을 입력해 주세요.</div>"
        )

    work_dir = tempfile.mkdtemp(prefix="dl_")
    outtmpl = os.path.join(work_dir, "%(playlist_index|)s%(playlist_index& - |)s%(title).150s.%(ext)s")

    ydl_opts = {
        "outtmpl": outtmpl,
        "noplaylist": _noplaylist_for(url),
        "windowsfilenames": True,
        "socket_timeout": DOWNLOAD_TIMEOUT,
        "retries": 2,
        "quiet": True,
        "no_warnings": True,
    }

    if fmt == "mp3":
        # 음성만 추출: 최고화질 오디오 트랙을 받아 mp3로 변환(ffmpeg 필요, Dockerfile에서 설치됨)
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "0",  # 가능한 최고 음질
            }
        ]
    else:
        # 영상+음성이 따로 나뉘어 제공되는 사이트(유튜브 등)에서 진짜 최고화질을 받으려면
        # "최고화질 영상 + 최고화질 음성을 합치기"를 먼저 시도하고, 그게 안 되는 사이트에서만
        # 이미 합쳐진 "best" 포맷으로 폴백해야 한다. 순서가 반대면(기존 버그) 유튜브 등에서
        # 화질이 낮은 사전 병합본(보통 720p 캡)을 먼저 받아버린다.
        ydl_opts["format"] = "bestvideo+bestaudio/best"
        ydl_opts["merge_output_format"] = "mp4"

    if COOKIE_FILE:
        ydl_opts["cookiefile"] = COOKIE_FILE

    last_error = None
    info = None
    # 429(요청 과다)는 일시적인 경우도 있어서 짧게 1번 더 재시도
    for attempt in range(2):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
            last_error = None
            break
        except Exception as e:
            last_error = e
            if attempt == 0 and "429" in str(e):
                time.sleep(4)
                continue
            break

    if last_error is not None:
        shutil.rmtree(work_dir, ignore_errors=True)
        msg = str(last_error)
        if "429" in msg:
            msg += (
                "\n\n(인스타그램이 이 서버의 요청을 일시적으로 막은 것일 수 있습니다. "
                "잠시 후 다시 시도해 주세요.)"
            )
        elif any(k in msg.lower() for k in ("login", "private", "authentication")):
            msg += (
                "\n\n(비공개 계정/스토리이거나 로그인이 필요한 콘텐츠일 수 있습니다. "
                "이 도구는 공개 게시물만 지원합니다.)"
            )
        return INDEX_HTML.format(
            style=PAGE_STYLE, pwa=PWA_HEAD, error=f"<div class=error>다운로드 실패:\n{msg}</div>"
        )

    # 임시 작업 폴더에 남은, yt-dlp의 부가 파일(.part/.json 등)을 제외한
    # 실제 미디어 파일들만 추린다. 캐러셀(여러 장) 게시물이면 파일이 여러 개 생긴다.
    junk_ext = (".part", ".ytdl", ".description", ".json", ".annotations.xml")
    files = [
        f
        for f in os.listdir(work_dir)
        if os.path.isfile(os.path.join(work_dir, f)) and not f.endswith(junk_ext)
    ]
    if not files:
        shutil.rmtree(work_dir, ignore_errors=True)
        return INDEX_HTML.format(
            style=PAGE_STYLE,
            pwa=PWA_HEAD,
            error="<div class=error>파일을 받지 못했습니다. 지원되지 않는 게시물 형식일 수 있습니다.</div>",
        )

    if len(files) > 1:
        # 캐러셀 등 여러 항목 -> zip으로 묶어서 하나의 파일로 전달
        zip_path = os.path.join(work_dir, "download.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(os.path.join(work_dir, f), arcname=f)
        result_path = zip_path
        title = None
        if isinstance(info, dict):
            title = info.get("title")
        download_name = _safe_filename((title or "download") + f"_{len(files)}items") + ".zip"
    else:
        result_path = os.path.join(work_dir, files[0])
        download_name = _safe_filename(os.path.basename(result_path))

    _schedule_cleanup(work_dir, delay=30.0)
    return send_file(result_path, as_attachment=True, download_name=download_name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
