FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY static/ static/

# Scan history lives here – mount a volume in production
VOLUME ["/app/data"]

# Cloud platforms inject PORT; default to 8765 for local Docker
ENV PORT=8765
ENV PYTHONUNBUFFERED=1

EXPOSE 8765

CMD ["python", "server.py"]
