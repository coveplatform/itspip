FROM python:3.12-slim

WORKDIR /app
ENV PIP_ENV=production \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Hosts (Render/Railway/Fly) inject $PORT; server.py reads it.
EXPOSE 8000
CMD ["python", "server.py"]
