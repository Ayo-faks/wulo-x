FROM python:3.13-slim

WORKDIR /app

COPY .agentops/prompts/inbound-assistant.prompt.md .agentops/prompts/inbound-assistant.prompt.md
COPY src/inbound-assistant/ src/inbound-assistant/

WORKDIR /app/src/inbound-assistant

RUN pip install --no-cache-dir --require-hashes -r requirements.lock

EXPOSE 8088

CMD ["python", "main.py"]