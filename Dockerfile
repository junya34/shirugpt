FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY src/ src/

# Cloud Run は PORT 環境変数でリッスンポートを指定し、コンテナは
# 0.0.0.0 で待ち受ける必要がある。サービスアカウントキーは焼き込まない
# （credentials/ は .dockerignore で除外）。認証は Cloud Run の
# 実行時サービスアカウントによる ADC に委ねる。
ENV PORT=8080
EXPOSE 8080

CMD streamlit run app.py \
    --server.port=${PORT} \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --browser.gatherUsageStats=false
