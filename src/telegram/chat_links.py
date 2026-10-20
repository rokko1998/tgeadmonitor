"""Parse Telegram references without making network requests."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}\Z")
INVITE = re.compile(r"[A-Za-z0-9_-]+\Z")
LINKS = re.compile(
    r"(?<![\w/@])(?:https?://(?:www\.)?(?:t\.me|telegram\.me)/[^\s<>\"'，,;]+"
    r"|(?:t\.me|telegram\.me)/[^\s<>\"'，,;]+"
    r"|tg://(?:join|resolve)\?[^\s<>\"'，,;]+|@[A-Za-z][A-Za-z0-9_]{3,31})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ChatLink:
    key: str
    kind: str
    target: str
    url: str


def extract_links(text: str) -> list[str]:
    return [m.group().rstrip(".!?)]}»、") for m in LINKS.finditer(text)]


def parse_link(raw: str) -> ChatLink:
    value = raw.strip().rstrip(".!?)]}»、")
    if value.startswith("@"):
        target, kind = value[1:], "public"
    else:
        if not re.match(r"^[a-z]+://", value, re.I):
            value = "https://" + value
        parsed = urlsplit(value)
        if parsed.scheme == "tg":
            query = parse_qs(parsed.query)
            if parsed.netloc == "join":
                target, kind = query.get("invite", [""])[0], "invite"
            elif parsed.netloc == "resolve":
                target, kind = query.get("domain", [""])[0], "public"
            else:
                raise ValueError("Поддерживаются tg://join и tg://resolve.")
        else:
            if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {
                "t.me", "www.t.me", "telegram.me", "www.telegram.me",
            }:
                raise ValueError("Нужна ссылка t.me на чат или @username.")
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 1 and parts[0].startswith("+"):
                target, kind = parts[0][1:], "invite"
            elif len(parts) == 2 and parts[0].lower() == "joinchat":
                target, kind = parts[1], "invite"
            else:
                if parts and parts[0].lower() == "s":
                    parts = parts[1:]
                if not parts or len(parts) > 2 or (len(parts) == 2 and not parts[1].isdigit()):
                    raise ValueError("Нужна ссылка на чат; эта ссылка ведёт в раздел Telegram.")
                target, kind = parts[0], "public"
                if target.lower() in {"c", "share", "addlist", "addstickers", "proxy", "socks"}:
                    raise ValueError("Для закрытого чата нужна пригласительная ссылка t.me/+…")
    if kind == "public":
        if not USERNAME.fullmatch(target):
            raise ValueError("Некорректный username Telegram-чата.")
        target = target.lower()
        return ChatLink(f"public:{target}", kind, target, f"https://t.me/{target}")
    if not INVITE.fullmatch(target):
        raise ValueError("Некорректная пригласительная ссылка.")
    return ChatLink(f"invite:{target}", kind, target, f"https://t.me/+{target}")
