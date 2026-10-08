FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# yt-dlp[default,deno]: 유튜브의 자바스크립트 챌린지를 풀기 위한 Deno 실행기와
# yt-dlp-ejs가 같이 설치된다. (이게 없으면 유튜브가 자주 실패함)
RUN pip install --no-cache-dir -U -r requirements.txt && deno --version
COPY app.py .
COPY static ./static

ENV PORT=8000
EXPOSE 8000
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "--timeout", "180", "app:app"]
