# Wan2GP Gateway Dedicated Dockerfile
FROM deepbeepmeep/wan2gp:latest

ENV PYTHONUNBUFFERED=1
ENV START_GATEWAY=1

WORKDIR /workspace

EXPOSE 50080

CMD ["python3", "gateway/gateway.py"]
