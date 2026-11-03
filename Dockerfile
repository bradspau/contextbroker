FROM python:3.14-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Run as an unprivileged user; the app files stay root-owned and read-only to it.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app
ENV PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "broker_engine.main:app", "--host", "0.0.0.0", "--port", "8080"]
