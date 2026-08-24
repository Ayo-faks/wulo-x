FROM python:3.13-slim

WORKDIR /app

COPY .agentops/prompts/recall-agent.prompt.md .agentops/prompts/recall-agent.prompt.md
COPY src/recall-agent/ src/recall-agent/

WORKDIR /app/src/recall-agent

RUN pip install --no-cache-dir --require-hashes -r requirements.lock

EXPOSE 8088

CMD ["python", "main.py"]