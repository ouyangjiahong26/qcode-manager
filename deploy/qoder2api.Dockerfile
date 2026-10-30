# qoder2api 的构建包装：内容与上游 Dockerfile 一致，仅给 pip 加国内镜像源
# （服务器出网到 PyPI 官方源超时；不改上游仓库文件，构建层适配）
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

COPY *.py ./
COPY baseprompt.json ./baseprompt.json

ENV QODER_HOST=0.0.0.0 \
    QODER_PORT=8963

EXPOSE 8963

CMD ["python", "-u", "openai_bridge.py"]
