# 编码/测试沙箱镜像：AI 生成的代码只在此容器内执行（网络默认关闭）
FROM python:3.9-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pytest==8.3.5

WORKDIR /work
# 默认无网络（docker run --network none），故不在运行时安装依赖
