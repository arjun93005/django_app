FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . demo_target/
EXPOSE 5050
# Single process on purpose: rate-limit state lives in memory.
CMD ["python", "-m", "demo_target.app"]
