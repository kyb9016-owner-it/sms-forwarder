#!/bin/bash
# SMS Forwarder - Hetzner 배포 스크립트
# 사용법: bash deploy.sh

set -e

echo "=== SMS Forwarder 배포 시작 ==="

# 1. 프로젝트 클론
cd /opt
if [ -d "sms-forwarder" ]; then
    echo "기존 디렉토리 발견, 업데이트 중..."
    cd sms-forwarder
    git pull origin main
else
    git clone https://github.com/kyb9016-owner-it/sms-forwarder.git
    cd sms-forwarder
fi

# 2. 환경변수 파일 생성
cat > server/.env << 'EOF'
AUTH_TOKEN=036f8832fd32e9ca7f066ac66f05beb4d41f47223186d6ee4672f4ab5cfe863a
TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
WECOM_ENABLED=true
WECOM_CORP_ID=wwc4d5910e9e90ed25
WECOM_SECRET=mblfQ2VSavypy4SuUszCmNUowM94RWKNjU9We3JZ7TE
WECOM_AGENT_ID=1000003
ALLOWED_SENDERS=
EOF

# 3. Docker로 빌드 & 실행
cd server
docker build -t sms-forwarder .
docker stop sms-forwarder 2>/dev/null || true
docker rm sms-forwarder 2>/dev/null || true
docker run -d \
    --name sms-forwarder \
    --restart unless-stopped \
    --env-file .env \
    -p 8000:8000 \
    sms-forwarder

echo ""
echo "=== 배포 완료 ==="
echo "서버 URL: http://YOUR_HETZNER_IP:8000"
echo "웹훅 URL: http://YOUR_HETZNER_IP:8000/webhook"
echo "헬스체크: http://YOUR_HETZNER_IP:8000/health"
echo ""
echo "※ HTTPS가 필요하면 Nginx + Let's Encrypt 설정이 필요합니다."
