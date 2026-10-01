FROM python:3.12-slim

WORKDIR /app
COPY jevkit/ ./jevkit/

EXPOSE 8787
CMD ["python3", "-m", "jevkit.server"]
