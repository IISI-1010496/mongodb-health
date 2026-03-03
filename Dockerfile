FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY snapshot.py analyze.py notify.py run.sh ./
COPY prompts/ prompts/
RUN chmod +x run.sh

ENTRYPOINT ["./run.sh"]
