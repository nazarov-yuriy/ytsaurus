# Baked image for the Python mock backend (alternative to the chart's
# run-from-ConfigMap default). Build from the iceberg-viewer/ directory:
#   docker build -f deploy/docker/mock-backend.Dockerfile \
#     -t registry.example/iceberg-ui-mock-backend:dev .
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "psycopg[binary]"
COPY mock-backend-py/server.py mock-backend-py/data.py mock-backend-py/webjson.py mock-backend-py/userdb.py ./
USER 65534:65534
EXPOSE 8000
HEALTHCHECK CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/ping')"]
CMD ["python3", "server.py", "8000"]
