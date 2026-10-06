#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
공개 게시물 전용 모바일 다운로드 웹앱
------------------------------------------------
- 인스타그램, 유튜브 등 yt-dlp가 지원하는 사이트의 "공개"(로그인 없이 누구나 볼 수 있는)
  게시물/영상만 대상으로 합니다.
- 로그인, 쿠키, 비공개 계정/스토리 접근 기능은 의도적으로 넣지 않았습니다.
  (스토리·비공개 콘텐츠는 인스타그램이 의도적으로 로그인 뒤에 막아둔 것이라
   "아이디만 넣으면 받아진다" 식의 도구는 만들지 않습니다.)
- 폰 브라우저로 어디서든 접속해서 쓰는 용도이므로, Render.com 같은 호스팅에
  올려서 공개 URL을 받는 걸 전제로 만들었습니다. (배포 방법은 README.md 참고)

로컬에서 테스트:
    pip install -r requirements.txt
    python app.py
    -> http://localhost:8000 접속 (같은 와이파이의 폰에서는 http://PC의사설IP:8000)

주의:
    - 본인 소유 콘텐츠 백업, 또는 저작권/초상권 문제가 없는 용도로만 사용하세요.
    - 다른 사람의 콘텐츠를 재배포하는 용도로 쓰지 마세요.
"""
import os
import re
import shutil
import tempfile
import threading
import time
import uuid

from flask import Flask, request, send_file, session, redirect, url_for

import yt_dlp

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", uuid.uuid4().hex)

# Render 등에 배포할 때 환경변수 APP_PASSWORD를 설정하면
# 아무나 내 공개 URL로 들어와서 남의 서버 자원을 쓰는 걸 막을 수 있습니다.
# (설정하지 않으면 비밀번호 없이 누구나 접속 가능하니, 외부에 공개하는 서버라면 꼭 설정하세요.)
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
DOWNLOAD_TIMEOUT = 60

PAGE_STYLE = """
<style>
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 480px; margin: 40px auto; padding: 0 16px; color:#222; }
  h1 { font-size: 20px; }
  input[type=text], input[type=password] { width: 100%; padding: 12px; font-size: 16px; box-sizing: border-box; border:1px solid #ccc; border-radius: 8px; margin-bottom: 12px; }
  button { width: 100%; padding: 14px; font-size: 16px; border: none; border-radius: 8px; background: #2563eb; color: white; }
  button:disabled { background: #999; }
  .note { font-size: 13px; color: #777; margin-top: 20px; line-height: 1.6; }
  .error { color: #c0392b; margin-top: 12px; white-space: pre-wrap; font-size: 14px; }
</style>
"""

LOGIN_HTML = """
<!doctype html><html><head><meta name=viewport content="width=device-width, initial-scale=1">
<title>로그인</title>{style}</head><body>
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
<title>공개 게시물 다운로드</title>{style}</head><body>
<h1>공개 게시물 다운로드</h1>
<form method=post action="/download" id=f>
<input type=text name=url placeholder="인스타그램/유튜브 등 공개 게시물 URL" autofocus>
<button type=submit id=btn>다운로드</button>
</form>
<script>
document.getElementById('f').addEventListener('submit', function(){{
  var b = document.getElementById('btn');
  b.disabled = true;
  b.textContent = '다운로드 중... (몇 초~수십 초 걸릴 수 있어요)';
}});
</script>
{error}
<div class=note>
공개(로그인 없이 누구나 볼 수 있는) 게시물/영상만 지원합니다.<br>
비공개 계정, 스토리, 로그인이 필요한 콘텐츠는 지원하지 않습니다.<br>
여러 장(캐러셀) 게시물은 현재 첫 번째 항목만 받아집니다.<br>
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
    return INDEX_HTML.format(style=PAGE_STYLE, error="")


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
    return LOGIN_HTML.format(style=PAGE_STYLE, error=error)


def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|\n\r\t]', "_", name).strip()
    return name[:150] or "download"


def _schedule_cleanup(path: str, delay: float = 30.0):
    def _cleanup():
        time.sleep(delay)
        shutil.rmtree(path, ignore_errors=True)

    threading.Thread(target=_cleanup, daemon=True).start()


@app.route("/download", methods=["POST"])
def download():
    if not check_auth():
        return redirect(url_for("login"))

    url = (request.form.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return INDEX_HTML.format(
            style=PAGE_STYLE, error="<div class=error>올바른 URL을 입력해 주세요.</div>"
        )

    work_dir = tempfile.mkdtemp(prefix="dl_")
    outtmpl = os.path.join(work_dir, "%(title).150s.%(ext)s")

    ydl_opts = {
        "format": "best/bestvideo+bestaudio",
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "windowsfilenames": True,
        "socket_timeout": DOWNLOAD_TIMEOUT,
        "retries": 2,
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info and "entries" in info and info["entries"]:
                info = info["entries"][0]
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        msg = str(e)
        if any(k in msg.lower() for k in ("login", "private", "rate-limit", "authentication")):
            msg += (
                "\n\n(비공개 계정/스토리이거나 로그인이 필요한 콘텐츠일 수 있습니다. "
                "이 도구는 공개 게시물만 지원합니다.)"
            )
        return INDEX_HTML.format(
            style=PAGE_STYLE, error=f"<div class=error>다운로드 실패:\n{msg}</div>"
        )

    files = [f for f in os.listdir(work_dir) if os.path.isfile(os.path.join(work_dir, f))]
    if not files:
        shutil.rmtree(work_dir, ignore_errors=True)
        return INDEX_HTML.format(
            style=PAGE_STYLE,
            error="<div class=error>파일을 받지 못했습니다. 지원되지 않는 게시물 형식일 수 있습니다.</div>",
        )

    # 썸네일 등 부수 파일이 같이 생겼을 경우를 대비해 가장 큰 파일(본편)을 고른다
    files.sort(key=lambda f: os.path.getsize(os.path.join(work_dir, f)), reverse=True)
    result_path = os.path.join(work_dir, files[0])
    download_name = _safe_filename(os.path.basename(result_path))

    _schedule_cleanup(work_dir, delay=30.0)
    return send_file(result_path, as_attachment=True, download_name=download_name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
