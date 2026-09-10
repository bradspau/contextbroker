FROM python:3.13-slim

WORKDIR /app
RUN pip install --no-cache-dir requests==2.34.2
COPY jsonp_proxy.py .

EXPOSE 8081
CMD ["python", "jsonp_proxy.py"]
