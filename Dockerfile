# Liara Docker platform: copy wheels first so pip can install offline.
FROM python:3.12-slim-bookworm

WORKDIR /usr/src/app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

COPY wheels/ /usr/src/app/wheels/
COPY requirements-base.txt /usr/src/app/requirements-base.txt

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir --no-index --find-links=/usr/src/app/wheels -r /usr/src/app/requirements-base.txt \
 && rm -rf /usr/src/app/wheels

COPY . /usr/src/app

EXPOSE 8000
CMD ["python", "main.py"]
