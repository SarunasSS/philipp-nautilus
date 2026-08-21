FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

COPY pyproject.toml README.md main.py ./
COPY cli ./cli
COPY src ./src
COPY strategies ./strategies

RUN pip install --no-cache-dir .

USER 10001:10001

CMD ["python", "main.py", "live", "subscribe", "run", "--bar-type", "MNQU6.GLBX-5-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL"]
