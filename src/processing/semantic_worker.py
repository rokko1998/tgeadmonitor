"""Async TypeSafe/B.AI adapter. Workflows and budget stay in application code."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone

import aiohttp

from src.processing.prefilter import opportunity_key, prefilter, semantic_text_hash
from src.processing.semantic_policy import VERSION, SemanticDecision, decide, load_questions, validate_response
from src.settings import Settings
from src.storage.database import Database
from src.storage.semantic import month_key
from src.storage.models import IncomingMessage, LeadRecord
from src.telegram.formatter import build_notification

log=logging.getLogger("semantic_worker")
ENDPOINTS={"typesafe":"https://api.typesafe.ai/v1/systemone","bai":"https://api.b.ai/v1/decisions"}


class SemanticWorker:
    def __init__(self, db: Database, settings: Settings, notifier, metrics):
        self.db,self.settings,self.notifier,self.metrics=db,settings,notifier,metrics
        self.questions,self.prompt_hash=load_questions(settings.config_dir)
        db.register_semantic_prompt(self.prompt_hash,VERSION,self.questions)
        self.blocked_until=0.0

    def notice(self, kind: str, text: str) -> None:
        self.db.enqueue_system(f"jev:{kind}:{month_key()}",{"text":text})
        self.notifier.wakeup()

    async def run(self) -> None:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
            while True:
                if not self.settings.jev_enabled or not self.settings.jev_api_key:
                    self.notice("not_configured","Проверка Jev ожидает настройки JEV_API_KEY/JEV_ENABLED. Сообщения сохраняются, лиды по старым баллам не рассылаются.")
                    await asyncio.sleep(30);continue
                if self.blocked_until>time.time():
                    await asyncio.sleep(min(30,self.blocked_until-time.time()));continue
                row=self.db.claim_semantic()
                if row is None:
                    await asyncio.sleep(.5);continue
                try:
                    await self.process(session,row)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Never log headers, tokens or provider response bodies.
                    log.error("Semantic job %s failed (%s)",row["message_id"],type(exc).__name__)
                    self.db.retry_semantic(row["message_id"],"internal",60,terminal=row["attempts"]>=5)
                    await asyncio.sleep(1)

    def complete(self, row, cache_key: str | None, decision: SemanticDecision,
                 answers: dict | None = None, duplicate: int | None = None) -> None:
        meta=json.loads(row["meta"])
        scope=(answers or {}).get("scope",{}).get("choice")
        product={"metal_material":"металлопрокат","lumber":"пиломатериалы",
                 "metal_service":"обработка / изготовление из металла",
                 "adjacent_service":"строительный контакт"}.get(scope)
        if decision.label=="indirect":product="строительный контакт / непрямая возможность"
        fields={"source_type":row["source_type"],"product":product or meta.get("product"),
                "location":meta.get("location"),"amount":meta.get("amount"),"size":meta.get("size")}
        date=datetime.fromisoformat(row["date"]) if row["date"] else datetime.now(timezone.utc)
        if date.tzinfo is None:date=date.replace(tzinfo=timezone.utc)
        message=IncomingMessage(chat_id=row["chat_id"],chat_title=row["chat_name"],
            telegram_message_id=row["telegram_message_id"],user_id=row["user_id"],username=row["username"],
            first_name=row["first_name"],text=row["original_text"],date=date,message_url=row["message_url"])
        def payload(lead_id):
            lead=LeadRecord(lead_id=lead_id,message_id=row["message_id"],message=message,
                lead_type="lead" if decision.label=="direct" else "indirect",score=0,
                explanation=decision.reason,found=[],**fields)
            return build_notification(lead,row["chat_name"])
        lead_id=self.db.complete_semantic(row["message_id"],cache_key,decision,VERSION,
            key=opportunity_key(row["original_text"]),duplicate=duplicate,
            lead_fields=fields,payload_builder=payload)
        if lead_id:
            self.metrics.inc("direct_leads" if decision.label=="direct" else "indirect_leads")
            self.notifier.wakeup()

    async def process(self, session: aiohttp.ClientSession, row) -> None:
        source=self.db.source(row["chat_id"])
        if not source or not source["enabled"] or not source["analysis_enabled"]:
            self.complete(row,None,SemanticDecision("archived","source_paused_archive",1));return
        reason=prefilter(row["original_text"])
        if reason:
            self.complete(row,None,SemanticDecision("ignore",reason,1));return
        duplicate=self.db.duplicate_opportunity(opportunity_key(row["original_text"]))
        if duplicate:
            lead=self.db.conn.execute("SELECT lead_type FROM leads WHERE id=?", (duplicate,)).fetchone()
            self.complete(row,None,SemanticDecision("indirect" if lead["lead_type"]=="indirect" else "direct","duplicate_project_repost",1),duplicate=duplicate)
            self.metrics.inc("semantic_duplicates");return
        payload={"model":self.settings.jev_model,"state":{"message":row["original_text"]},"questions":self.questions}
        raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":"))
        cache_key=hashlib.sha256((self.settings.jev_provider+":"+VERSION+":"+raw).encode()).hexdigest()
        cached=self.db.cached_semantic(cache_key)
        if cached:
            data=json.loads(cached["response"])
            self.complete(row,cache_key,decide(data["answers"]),data["answers"])
            self.metrics.inc("semantic_cache_hits");return
        reserve_tokens=len(raw.encode())*2+8192
        attempt=self.db.reserve_semantic(cache_key,reserve_tokens,int(self.settings.jev_monthly_budget_usd*1_000_000_000))
        if attempt is None:
            self.db.retry_semantic(row["message_id"],"monthly_budget",60)
            self.blocked_until=time.time()+60
            self.notice("budget","Достигнут лимит расходов Jev. Неоценённые сообщения сохранены в очереди; рассылка по старому score не включается.")
            return
        start=time.monotonic()
        try:
            async with session.post(ENDPOINTS[self.settings.jev_provider],json=payload,
                    headers={"Authorization":"Bearer "+self.settings.jev_api_key},allow_redirects=False) as response:
                code=response.status
                if code!=200:
                    self.db.finish_semantic_attempt(attempt,input_tokens=0 if code==429 else None,state="http_error",error=str(code))
                    try:delay=min(3600,max(5,float(response.headers.get("retry-after","30"))))
                    except ValueError:delay=30
                    self.db.retry_semantic(row["message_id"],f"http_{code}",delay,
                                           terminal=code in (400,401,403,404) or row["attempts"]>=5)
                    self.blocked_until=time.time()+(delay if code==429 else 300)
                    self.notice(f"http_{code}",f"Jev временно недоступен (HTTP {code}). Сообщения сохраняются; автоматический пропуск проверки отключён.")
                    return
                data=await response.json(content_type=None)
            usage=data.get("usage",{}).get("input_tokens")
            valid_usage=isinstance(usage,int) and not isinstance(usage,bool) and usage>=0
            self.db.finish_semantic_attempt(attempt,input_tokens=usage if valid_usage else None,state="response")
            validate_response(data,self.questions)
            if data["model"]!=self.settings.jev_model:
                raise ValueError("Unexpected resolved model")
            data={k:data[k] for k in ("model","answers","usage")}
            self.db.save_semantic_evidence(cache_key,self.settings.jev_provider,self.settings.jev_model,
                self.prompt_hash,VERSION,semantic_text_hash(row["original_text"]),data,time.monotonic()-start)
            self.complete(row,cache_key,decide(data["answers"]),data["answers"])
            self.metrics.inc("semantic_api_success")
        except (aiohttp.ClientError,asyncio.TimeoutError) as exc:
            self.db.finish_semantic_attempt(attempt,input_tokens=None,state="uncertain_transport",error=type(exc).__name__)
            self.db.retry_semantic(row["message_id"],"transport",min(300,10*2**min(row["attempts"],5)),terminal=row["attempts"]>=5)
        except (ValueError,TypeError,KeyError):
            self.db.retry_semantic(row["message_id"],"invalid_response",300,terminal=True)
            self.blocked_until=time.time()+300
            self.notice("invalid_response","Jev вернул неподходящий ответ. Сообщение сохранено для проверки, уведомление не отправлено.")
