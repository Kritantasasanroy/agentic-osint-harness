FROM python:3.12-slim

RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH" \
    PYTHONPATH="/app/src" \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --chown=user web/backend/requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

COPY --chown=user src ./src
COPY --chown=user benchmark ./benchmark
COPY --chown=user web/backend/app.py .

EXPOSE 7860
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-7860}"]
