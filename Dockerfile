FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 ADAGENT_DB_URL=sqlite:////data/adagent.db
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY adagent ./adagent
VOLUME /data
EXPOSE 8000
CMD ["python", "-m", "adagent", "serve", "--port", "8000"]
