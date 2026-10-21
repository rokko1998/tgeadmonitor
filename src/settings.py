"""Секреты и runtime-настройки. Только из окружения (.env), никогда из кода."""
from __future__ import annotations

import os
import math
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class SettingsError(Exception):
    pass


def _req(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SettingsError(f"Environment variable {name} is required")
    return value


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    session: str
    bot_token: str
    owner_chat_id: int
    config_dir: Path
    data_dir: Path
    db_path: Path
    log_level: str
    store_all_messages: bool
    jev_enabled: bool = True
    jev_provider: str = "typesafe"
    jev_api_key: str = field(default="",repr=False)
    jev_model: str = "jev-1.14.0"
    jev_monthly_budget_usd: float = 4.0
    digest_hour_msk: int = 21
    monthly_report_hour_msk: int = 9
    source_quality_days: int = 30
    source_quality_min_messages: int = 1000

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("DATA_DIR", "data"))
        provider=os.getenv("JEV_PROVIDER","typesafe").lower().strip()
        if provider not in {"typesafe","bai"}:
            raise SettingsError("JEV_PROVIDER must be typesafe or bai")
        model=os.getenv("JEV_MODEL","jev-1.14.0" if provider=="typesafe" else "jev-1.13.0").strip()
        if model not in ({"jev-1.13.0","jev-1.14.0"} if provider=="typesafe" else {"jev-1.13.0"}):
            raise SettingsError("Use a pinned model supported by JEV_PROVIDER")
        try:
            budget=float(os.getenv("JEV_MONTHLY_BUDGET_USD","4"))
            digest_hour=int(os.getenv("DIGEST_HOUR_MSK","21"))
            monthly_hour=int(os.getenv("MONTHLY_REPORT_HOUR_MSK","9"))
            quality_days=int(os.getenv("SOURCE_QUALITY_DAYS","30"))
            quality_min=int(os.getenv("SOURCE_QUALITY_MIN_MESSAGES","1000"))
        except ValueError as exc:
            raise SettingsError("Invalid Jev/digest/source settings") from exc
        if (not math.isfinite(budget) or budget<0 or not 0<=digest_hour<=23
                or not 0<=monthly_hour<=23 or quality_days<1 or quality_min<1):
            raise SettingsError("Invalid Jev/digest/source limits")
        return cls(
            api_id=int(_req("API_ID")),
            api_hash=_req("API_HASH"),
            session=os.getenv("SESSION", "").strip(),
            bot_token=_req("BOT_TOKEN"),
            owner_chat_id=int(_req("OWNER_CHAT_ID")),
            config_dir=Path(os.getenv("CONFIG_DIR", "config")),
            data_dir=data_dir,
            db_path=Path(os.getenv("DB_PATH", str(data_dir / "leads.db"))),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            store_all_messages=os.getenv("STORE_ALL_MESSAGES", "1") == "1",
            jev_enabled=os.getenv("JEV_ENABLED","1")=="1",jev_provider=provider,
            jev_api_key=os.getenv("JEV_API_KEY","").strip(),jev_model=model,
            jev_monthly_budget_usd=budget,digest_hour_msk=digest_hour,
            monthly_report_hour_msk=monthly_hour,
            source_quality_days=quality_days,source_quality_min_messages=quality_min,
        )
