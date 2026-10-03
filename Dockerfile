# A FieldMind edge device on Linux. The same code runs on a Raspberry Pi or an
# industrial gateway: qdrant-edge-py ships wheels for x86_64 and aarch64.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY fieldmind fieldmind

ENV FIELDMIND_HOST=0.0.0.0 \
    FIELDMIND_PORT=7860 \
    FIELDMIND_DATA=/data \
    FIELDMIND_MODELS=/models

EXPOSE 7860
CMD ["python", "-m", "fieldmind", "serve"]
