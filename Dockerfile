FROM python:3.12-slim

WORKDIR /app

# Python libraries + the headless Chromium browser (with the system libraries it needs)
RUN pip install --no-cache-dir playwright requests \
    && playwright install --with-deps chromium

COPY restock_server.py .

CMD ["python", "-u", "restock_server.py"]
