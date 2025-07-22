# Dockerfile

FROM python:3.11-slim

# 安装 docker 客户端所需的依赖
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir flask docker

EXPOSE 5000
CMD ["python", "main.py"]
