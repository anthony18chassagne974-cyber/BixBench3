FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py index.html ./
ENV PYTHONUNBUFFERED=1
USER 65532:65532
CMD ["python", "server.py", "--source", "gs://bixbench3-task11-jex20-results/_dashboard"]
