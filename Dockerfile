FROM python:3.12-slim

WORKDIR /app
COPY jev_review.py jev_evidence.py server.py ./

EXPOSE 8787
CMD ["python3", "server.py"]
