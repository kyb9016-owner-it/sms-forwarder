"""
SMS 인증번호 전달 서버
iPhone 단축어에서 웹훅을 받아 Telegram/WeCom으로 전달합니다.
"""

import base64
import collections
import hashlib
import logging
import os
import re
import struct
from datetime import datetime, timezone, timedelta

import httpx
from Crypto.Cipher import AES
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
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
WECOM_CORP_ID: str = os.environ.get("WECOM_CORP_ID", "")
WECOM_AGENT_ID: str = os.environ.get("WECOM_AGENT_ID", "")
WECOM_SECRET: str = os.environ.get("WECOM_SECRET", "")
WECOM_TO_USER: str = os.environ.get("WECOM_TO_USER", "")
WECOM_TOKEN: str = os.environ.get("WECOM_TOKEN", "")
WECOM_ENCODING_AES_KEY: str = os.environ.get("WECOM_ENCODING_AES_KEY", "")

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

# ── 인증번호 히스토리 (최근 20개) ────────────────────────────────────────────
code_history: collections.deque = collections.deque(maxlen=20)

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


def _wecom_decrypt_echostr(encrypted: str, encoding_aes_key: str) -> str:
    key = base64.b64decode(encoding_aes_key + "=")
    cipher = AES.new(key, AES.MODE_CBC, key[:16])
    decrypted = cipher.decrypt(base64.b64decode(encrypted))
    pad = decrypted[-1]
    decrypted = decrypted[:-pad]
    msg_len = struct.unpack(">I", decrypted[16:20])[0]
    return decrypted[20:20 + msg_len].decode("utf-8")


def _wecom_verify_signature(token: str, timestamp: str, nonce: str, echostr: str, msg_signature: str) -> bool:
    items = sorted([token, timestamp, nonce, echostr])
    signature = hashlib.sha1("".join(items).encode()).hexdigest()
    return signature == msg_signature


async def get_wecom_access_token(client: httpx.AsyncClient) -> str | None:
    url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
    params = {"corpid": WECOM_CORP_ID, "corpsecret": WECOM_SECRET}
    try:
        resp = await client.get(url, params=params)
        data = resp.json()
        if data.get("errcode") == 0:
            return data["access_token"]
        logger.error("WeCom 토큰 발급 실패: %s", data)
        return None
    except httpx.HTTPError as e:
        logger.error("WeCom 토큰 요청 오류: %s", e)
        return None


async def send_wecom(message: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            token = await get_wecom_access_token(client)
            if not token:
                return False
            url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
            payload = {
                "touser": WECOM_TO_USER,
                "msgtype": "text",
                "agentid": int(WECOM_AGENT_ID),
                "text": {"content": message},
            }
            resp = await client.post(url, json=payload)
            data = resp.json()
            if data.get("errcode") == 0:
                logger.info("WeCom 전송 성공")
                return True
            logger.error("WeCom 전송 실패: %s", data)
            return False
    except httpx.HTTPError as e:
        logger.error("WeCom 전송 오류: %s", e)
        return False


# ── 엔드포인트 ────────────────────────────────────────────────────────────────

@app.get("/wecom/callback")
async def wecom_callback_verify(
    msg_signature: str,
    timestamp: str,
    nonce: str,
    echostr: str,
):
    if not _wecom_verify_signature(WECOM_TOKEN, timestamp, nonce, echostr, msg_signature):
        raise HTTPException(status_code=403, detail="서명 검증 실패")
    plaintext = _wecom_decrypt_echostr(echostr, WECOM_ENCODING_AES_KEY)
    logger.info("WeCom 콜백 검증 성공")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(plaintext)


@app.get("/codes", response_class=HTMLResponse)
async def codes_page():
    today = datetime.now(tz=KST).strftime("%Y-%m-%d")
    items_html = ""
    for entry in code_history:
        if not entry["time"].startswith(today):
            continue
        sender = entry['sender']
        if len(sender) >= 4:
            sender = sender[:3] + "****" + sender[-4:]
        items_html += f"""
        <div class="card">
            <div class="code">{entry['code']}</div>
            <div class="meta">발신: {sender}</div>
            <div class="meta">{entry['message']}</div>
            <div class="time">{entry['time']}</div>
        </div>"""
    if not items_html:
        items_html = '<div class="empty">수신된 인증번호가 없습니다.</div>'

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>인증번호</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, sans-serif; background: #f5f5f5; padding: 16px; }}
  h1 {{ font-size: 18px; color: #333; margin-bottom: 16px; text-align: center; }}
  .card {{ background: #fff; border-radius: 12px; padding: 16px; margin-bottom: 12px; box-shadow: 0 1px 4px rgba(0,0,0,0.1); }}
  .code {{ font-size: 36px; font-weight: bold; color: #1677ff; letter-spacing: 4px; text-align: center; margin-bottom: 8px; }}
  .meta {{ font-size: 13px; color: #666; margin-top: 4px; }}
  .time {{ font-size: 12px; color: #999; margin-top: 6px; }}
  .empty {{ text-align: center; color: #999; margin-top: 40px; }}
  .refresh-btn {{ display: block; width: 100%; padding: 12px; background: #1677ff; color: #fff; border: none; border-radius: 8px; font-size: 15px; cursor: pointer; margin-top: 8px; }}
</style>
</head>
<body>
<h1>🔐 인증번호</h1>
<button class="refresh-btn" onclick="location.reload()">새로고침</button>
{items_html}
</body>
</html>"""


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

    code_history.appendleft({
        "code": code,
        "sender": body.sender,
        "message": body.message.strip()[:100],
        "time": now_kst.strftime("%Y-%m-%d %H:%M:%S"),
    })

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
