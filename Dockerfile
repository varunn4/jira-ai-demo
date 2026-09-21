FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client ca-certificates curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && git config --system --add safe.directory '*' \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g repomix \
    && npm cache clean --force

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Build the React admin SPA into frontend/dist so FastAPI can serve it at /.
RUN cd frontend \
    && npm install --no-audit --no-fund \
    && npm run build \
    && rm -rf node_modules \
    && npm cache clean --force

EXPOSE 8000

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--ws", "wsproto"]
