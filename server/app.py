"""
SMS 인증번호 전달 서버
iPhone 단축어에서 웹훅을 받아 Telegram/WeCom으로 전달합니다.
"""

import logging
import os
import re
from datetime import datetime, timezone, timedelta

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel

load_dotenv()

# ── 타임존 ──────────────────────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))

# ── 로거 ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── 환경변수 ─────────────────────────────────────────────────────────────────
AUTH_TOKEN: str = os.environ.get("AUTH_TOKEN", "")
TELEGRAM_ENABLED: bool = os.environ.get("TELEGRAM_ENABLED", "false").lower() == "true"
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")
WECOM_ENABLED: bool = os.environ.get("WECOM_ENABLED", "false").lower() == "true"
WECOM_WEBHOOK_URL: str = os.environ.get("WECOM_WEBHOOK_URL", "")
WECOM_CORP_ID: str = os.environ.get("WECOM_CORP_ID", "")
WECOM_SECRET: str = os.environ.get("WECOM_SECRET", "")
WECOM_AGENT_ID: str = os.environ.get("WECOM_AGENT_ID", "")

_raw_senders = os.environ.get("ALLOWED_SENDERS", "").strip()
ALLOWED_SENDERS: list[str] = [s.strip() for s in _raw_senders.split(",") if s.strip()] if _raw_senders else []

# ── 인증번호 추출 패턴 ───────────────────────────────────────────────────────
PATTERNS: list[str] = [
    r"인증번호[^\d]*(\d{4,8})",
    r"인증코드[^\d]*(\d{4,8})",
    r"확인코드[^\d]*(\d{4,8})",
    r"본인확인[^\d]*(\d{4,8})",
    r"verification\s*code[^\d]*(\d{4,8})",
    r"(?:^|[^0-9])(\d{6})(?:[^0-9]|$)",  # 6자리 독립 숫자 (마지막 수단)
]

# ── FastAPI ─────────────────────────────────────────────────────────────────
app = FastAPI(title="SMS 인증번호 전달 서버", docs_url=None, redoc_url=None)


# ── 모델 ─────────────────────────────────────────────────────────────────────
class WebhookRequest(BaseModel):
    sender: str
    message: str
    token: str


# ── 헬퍼 함수 ────────────────────────────────────────────────────────────────

def normalize_phone(number: str) -> str:
    """비교를 위해 전화번호에서 숫자 외 문자를 제거합니다."""
    return re.sub(r"[^0-9]", "", number)


def is_allowed_sender(sender: str, allowed: list[str]) -> bool:
    """발신번호가 허용 목록에 있는지 확인합니다. 목록이 비어있으면 모두 허용."""
    if not allowed:
        return True
    normalized = normalize_phone(sender)
    return any(
        normalize_phone(a) in normalized or normalized in normalize_phone(a)
        for a in allowed
    )


def extract_code(text: str) -> str | None:
    """메시지에서 인증번호를 추출합니다."""
    for pattern in PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def format_message(sender: str, code: str, text: str, dt: datetime) -> str:
    snippet = text.strip()
    if len(snippet) > 80:
        snippet = snippet[:77] + "..."
    time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
    return (
        "🔐 인증번호 감지\n"
        f"발신: {sender}\n"
        f"코드: {code}\n"
        f"원문: {snippet}\n"
        f"시각: {time_str}"
    )


async def send_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code == 200:
            logger.info("Telegram 전송 성공")
            return True
        else:
            logger.error("Telegram 전송 실패: %s %s", resp.status_code, resp.text)
            return False
    except httpx.HTTPError as e:
        logger.error("Telegram 전송 오류: %s", e)
        return False


async def get_wecom_access_token() -> str | None:
    """WeCom 자체 앱 API용 access_token을 가져옵니다."""
    url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
    params = {"corpid": WECOM_CORP_ID, "corpsecret": WECOM_SECRET}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("errcode") == 0:
                return data["access_token"]
            else:
                logger.error("WeCom access_token 획득 실패: %s", data)
                return None
        else:
            logger.error("WeCom access_token 요청 실패: %s", resp.status_code)
            return None
    except httpx.HTTPError as e:
        logger.error("WeCom access_token 오류: %s", e)
        return None


async def send_wecom_app(message: str) -> bool:
    """WeCom 자체 앱 API를 통해 메시지를 전송합니다."""
    access_token = await get_wecom_access_token()
    if not access_token:
        return False
    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}"
    payload = {
        "touser": "@all",
        "msgtype": "text",
        "agentid": int(WECOM_AGENT_ID),
        "text": {"content": message},
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("errcode") == 0:
                logger.info("WeCom 앱 전송 성공")
                return True
            else:
                logger.error("WeCom 앱 전송 실패: %s", data)
                return False
        else:
            logger.error("WeCom 앱 전송 실패: %s %s", resp.status_code, resp.text)
            return False
    except httpx.HTTPError as e:
        logger.error("WeCom 앱 전송 오류: %s", e)
        return False


async def send_wecom_webhook(message: str) -> bool:
    """WeCom 웹훅 URL을 통해 메시지를 전송합니다."""
    payload = {"msgtype": "text", "text": {"content": message}}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(WECOM_WEBHOOK_URL, json=payload)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("errcode") == 0:
                logger.info("WeCom 웹훅 전송 성공")
                return True
            else:
                logger.error("WeCom 웹훅 전송 실패: %s", data)
                return False
        else:
            logger.error("WeCom 웹훅 전송 실패: %s %s", resp.status_code, resp.text)
            return False
    except httpx.HTTPError as e:
        logger.error("WeCom 웹훅 전송 오류: %s", e)
        return False


async def send_wecom(message: str) -> bool:
    """WeCom으로 메시지를 전송합니다. 앱 API 우선, 없으면 웹훅 사용."""
    if WECOM_CORP_ID and WECOM_SECRET and WECOM_AGENT_ID:
        return await send_wecom_app(message)
    elif WECOM_WEBHOOK_URL:
        return await send_wecom_webhook(message)
    else:
        logger.error("WeCom 설정이 없습니다. 앱 API 또는 웹훅 URL을 설정하세요.")
        return False


# ── 엔드포인트 ────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "정상", "message": "SMS 인증번호 전달 서버가 실행 중입니다."}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "telegram": "활성화" if TELEGRAM_ENABLED else "비활성화",
        "wecom": "활성화" if WECOM_ENABLED else "비활성화",
        "allowed_senders": ALLOWED_SENDERS if ALLOWED_SENDERS else "전체 허용",
    }


@app.post("/webhook")
async def webhook(body: WebhookRequest):
    logger.info("웹훅 수신 | 발신: %s | 메시지: %s", body.sender, body.message[:50])

    # 인증 토큰 확인
    if not AUTH_TOKEN:
        logger.error("AUTH_TOKEN이 설정되지 않았습니다.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="서버 설정 오류: AUTH_TOKEN이 없습니다.",
        )
    if body.token != AUTH_TOKEN:
        logger.warning("인증 실패 | 발신: %s", body.sender)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="인증 토큰이 올바르지 않습니다.",
        )

    # 발신번호 필터
    if not is_allowed_sender(body.sender, ALLOWED_SENDERS):
        logger.info("발신번호 필터링 | 발신: %s", body.sender)
        return {"status": "filtered", "message": "허용되지 않은 발신번호입니다."}

    # 인증번호 추출
    code = extract_code(body.message)
    if not code:
        logger.info("인증번호 없음 | 발신: %s | 메시지: %s", body.sender, body.message[:50])
        return {"status": "skipped", "message": "인증번호를 찾을 수 없습니다."}

    now_kst = datetime.now(tz=KST)
    formatted = format_message(body.sender, code, body.message, now_kst)
    logger.info("인증번호 감지 | 발신: %s | 코드: %s", body.sender, code)

    results: dict[str, str] = {}

    if TELEGRAM_ENABLED:
        ok = await send_telegram(formatted)
        results["telegram"] = "성공" if ok else "실패"
    else:
        results["telegram"] = "비활성화"

    if WECOM_ENABLED:
        ok = await send_wecom(formatted)
        results["wecom"] = "성공" if ok else "실패"
    else:
        results["wecom"] = "비활성화"

    logger.info("전달 결과 | %s", results)
    return {"status": "forwarded", "code": code, "results": results}
