# 使用官方轻量级 Python 镜像
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 设置时区为上海时间 (可选，方便看日志)
RUN apt-get update && apt-get install -y tzdata \
    && ln -fs /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && dpkg-reconfigure -f noninteractive tzdata \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制所有源代码到容器中
COPY . .

# 设置环境变量，确保 Python 输出直接打印到日志，不被缓存
ENV PYTHONUNBUFFERED=1

# 启动命令 (默认运行 Hyperliquid 机器人)
CMD ["python", "chua_Hyperliquid.py"]
