#!/usr/bin/env python3
"""
SMS 인증번호 자동 전달 스크립트
macOS Messages DB를 감시하여 인증번호를 Telegram 및 WeCom으로 전달합니다.
"""

import argparse
import logging
import os
import re
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
import yaml

# Apple CoreData 기준 시각: 2001-01-01 00:00:00 UTC
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
# Apple timestamp 단위: 나노초
APPLE_TIMESTAMP_SCALE = 1_000_000_000

KST = timezone(timedelta(hours=9))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

_running = True


def handle_signal(signum, frame):
    global _running
    logger.info("종료 신호 수신 (signal %d). 종료 중...", signum)
    _running = False


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def apple_timestamp_to_datetime(ts: int) -> datetime:
    """Apple CoreData 나노초 타임스탬프를 KST datetime으로 변환합니다."""
    seconds = ts / APPLE_TIMESTAMP_SCALE
    dt_utc = APPLE_EPOCH + timedelta(seconds=seconds)
    return dt_utc.astimezone(KST)


def open_db(db_path: str) -> sqlite3.Connection:
    expanded = os.path.expanduser(db_path)
    uri = f"file:{expanded}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def get_max_rowid(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message")
    row = cur.fetchone()
    return row[0] if row else 0


def fetch_new_messages(conn: sqlite3.Connection, since_rowid: int) -> list[dict]:
    query = """
        SELECT
            message.ROWID,
            message.text,
            message.date,
            message.is_from_me,
            handle.id AS sender
        FROM message
        LEFT JOIN handle ON message.handle_id = handle.ROWID
        WHERE message.ROWID > ?
          AND message.is_from_me = 0
        ORDER BY message.ROWID ASC
    """
    cur = conn.execute(query, (since_rowid,))
    rows = cur.fetchall()
    return [
        {
            "rowid": r[0],
            "text": r[1] or "",
            "date": r[2],
            "sender": r[4] or "알 수 없음",
        }
        for r in rows
    ]


def normalize_phone(number: str) -> str:
    """비교를 위해 전화번호에서 특수문자를 제거합니다."""
    return re.sub(r"[^0-9]", "", number)


def is_allowed_sender(sender: str, allowed: list[str]) -> bool:
    """발신번호가 허용 목록에 있는지 확인합니다. 목록이 비어있으면 모두 허용."""
    if not allowed:
        return True
    normalized = normalize_phone(sender)
    return any(normalize_phone(a) in normalized or normalized in normalize_phone(a) for a in allowed)


def extract_code(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
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


def send_telegram(token: str, chat_id: str, message: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info("Telegram 전송 성공")
            return True
        else:
            logger.error("Telegram 전송 실패: %s %s", resp.status_code, resp.text)
            return False
    except requests.RequestException as e:
        logger.error("Telegram 전송 오류: %s", e)
        return False


def send_wecom(webhook_url: str, message: str) -> bool:
    payload = {"msgtype": "text", "text": {"content": message}}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("errcode") == 0:
                logger.info("WeCom 전송 성공")
                return True
            else:
                logger.error("WeCom 전송 실패: %s", data)
                return False
        else:
            logger.error("WeCom 전송 실패: %s %s", resp.status_code, resp.text)
            return False
    except requests.RequestException as e:
        logger.error("WeCom 전송 오류: %s", e)
        return False


def print_banner(cfg: dict):
    tg = cfg.get("telegram", {})
    wc = cfg.get("wecom", {})
    watch = cfg.get("watch", {})
    interval = watch.get("interval_seconds", 2)
    db_path = watch.get("messages_db", "~/Library/Messages/chat.db")

    allowed_senders = cfg.get("senders", []) or []
    tg_status = "활성화" if tg.get("enabled") else "비활성화"
    wc_status = "활성화" if wc.get("enabled") else "비활성화"
    sender_status = ", ".join(allowed_senders) if allowed_senders else "전체 (필터 없음)"

    print("=" * 50)
    print("  SMS 인증번호 자동 전달 시작")
    print("=" * 50)
    print(f"  Telegram : {tg_status}")
    print(f"  WeCom    : {wc_status}")
    print(f"  감시 번호: {sender_status}")
    print(f"  DB 경로  : {db_path}")
    print(f"  폴링 주기: {interval}초")
    print("=" * 50)


def run(cfg: dict):
    watch = cfg.get("watch", {})
    db_path = watch.get("messages_db", "~/Library/Messages/chat.db")
    interval = watch.get("interval_seconds", 2)
    patterns = cfg.get("patterns", [])

    allowed_senders = cfg.get("senders", []) or []
    tg_cfg = cfg.get("telegram", {})
    wc_cfg = cfg.get("wecom", {})

    try:
        conn = open_db(db_path)
    except sqlite3.OperationalError as e:
        logger.error(
            "DB 열기 실패: %s\n"
            "  -> 시스템 환경설정 > 개인 정보 보호 > 전체 디스크 접근에서\n"
            "     터미널(또는 실행 앱)에 권한을 부여하세요.",
            e,
        )
        sys.exit(1)

    last_rowid = get_max_rowid(conn)
    logger.info("시작 ROWID: %d — 이후 새 메시지만 감시합니다.", last_rowid)

    while _running:
        try:
            # 연결을 매 폴링마다 새로 열어서 WAL 체크포인트 이후 변경사항을 반영합니다.
            conn.close()
            conn = open_db(db_path)
            messages = fetch_new_messages(conn, last_rowid)
        except sqlite3.OperationalError as e:
            logger.error("DB 읽기 오류: %s", e)
            time.sleep(interval)
            continue

        for msg in messages:
            last_rowid = msg["rowid"]
            text = msg["text"]
            if not text:
                continue

            if not is_allowed_sender(msg["sender"], allowed_senders):
                continue

            code = extract_code(text, patterns)
            if not code:
                continue

            dt = apple_timestamp_to_datetime(msg["date"])
            formatted = format_message(msg["sender"], code, text, dt)

            logger.info(
                "인증번호 감지 | 발신: %s | 코드: %s | ROWID: %d",
                msg["sender"],
                code,
                msg["rowid"],
            )

            if tg_cfg.get("enabled"):
                send_telegram(tg_cfg["bot_token"], tg_cfg["chat_id"], formatted)

            if wc_cfg.get("enabled"):
                send_wecom(wc_cfg["webhook_url"], formatted)

        time.sleep(interval)

    conn.close()
    logger.info("종료 완료.")


def main():
    parser = argparse.ArgumentParser(description="SMS 인증번호 자동 전달")
    parser.add_argument(
        "--config",
        default="config.yaml",
        metavar="FILE",
        help="설정 파일 경로 (기본값: config.yaml)",
    )
    args = parser.parse_args()

    config_path = os.path.expanduser(args.config)
    if not os.path.exists(config_path):
        print(f"오류: 설정 파일을 찾을 수 없습니다 — {config_path}")
        print("  config.example.yaml을 복사하여 config.yaml을 만들고 값을 채워주세요.")
        sys.exit(1)

    cfg = load_config(config_path)
    print_banner(cfg)
    run(cfg)


if __name__ == "__main__":
    main()
