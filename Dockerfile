# ashare-anomaly-prober 容器化部署（可选）
# 构建: docker build -t ashare-prober .
# 运行: docker run -p 8000:8000 -v $(pwd)/config.toml:/app/config.toml \
#             -v prober-data:/app/data -v prober-outputs:/app/outputs ashare-prober
#
# 依赖版本一律以 requirements.lock.txt 为准（含 akshare==1.18.84 锁定）。
# ⚠️ 升级基础镜像 / akshare 前，先读 README「版本锁定与升级注意事项」。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先装 lock 依赖（利用层缓存；lock 即验证过的版本基线）
COPY requirements.lock.txt ./
RUN pip install -r requirements.lock.txt

# 再装本包（--no-deps：依赖一律以 lock 为准，避免重新解析引入漂移）
COPY pyproject.toml README.md LICENSE* ./
COPY src ./src
RUN pip install --no-deps .

# 数据库 / 产物 / 配置走挂载卷，容器可随时重建
RUN mkdir -p /app/data /app/outputs
VOLUME ["/app/data", "/app/outputs"]

EXPOSE 8000
CMD ["prober", "serve", "--port", "8000"]
