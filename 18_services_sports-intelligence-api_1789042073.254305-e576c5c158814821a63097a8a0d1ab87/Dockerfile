FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    APP_ROOT=/app \
    DATA_DIR=data \
    CHROMA_DIR=/tmp/chroma \
    MODEL_DIR=data/models \
    GOOGLE_GENAI_USE_VERTEXAI=true \
    GCP_PROJECT=mos-gcp-host-dev-prj \
    VERTEX_LOCATION=global \
    ENABLE_VECTOR_SEARCH=false

WORKDIR /app
COPY backend/requirements.cloud.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY data/raw ./data/raw
COPY data/derived ./data/derived

CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
