FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/backend
COPY pyproject.toml requirements.lock ./
COPY anymob ./anymob
RUN pip install --no-cache-dir -c requirements.lock . && useradd --create-home --uid 10001 anymob
COPY alembic.ini ./
COPY migrations ./migrations
USER anymob
EXPOSE 8000
CMD ["uvicorn", "anymob.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
