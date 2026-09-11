# Python·패키지 버전을 고정한다. 고정하지 않으면 재빌드할 때마다 새 버전을 받아
# 측정 대상 서버가 바뀔 수 있다. 버전은 2026-09-12 실행 중이던 이미지 기준이다 (#74).
FROM python:3.11.16-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
