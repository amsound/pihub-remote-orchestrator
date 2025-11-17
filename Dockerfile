
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc python3-dev linux-libc-dev \
    libevdev2 libevdev-dev \
    bluez bluez-tools dbus libglib2.0-0 \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY pihub /app/pihub

ENV DATA_DIR=/data
VOLUME ["/data"]

# host networking is recommended (docker run --net=host)
CMD ["python","-m","pihub.app"]
