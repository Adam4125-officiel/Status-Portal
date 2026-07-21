FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p instance && useradd -m -u 1000 portal && chown -R portal:portal /app
USER portal

EXPOSE 5000

CMD ["python", "serve_waitress.py"]
