from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import agent, identification, rag as rag_mod, sentiment, tools
from .db import engine, get_db
from .models import Base, Message, Session as ChatSession
from .rag import ensure_collection, ensure_policy_collection
from .schemas import ChatRequest, ChatResponse, HealthResponse, RagSource
from .settings import get_settings

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    try:
        ensure_collection()
    except Exception as exc:
        log.warning("Qdrant catalog collection not ready at startup: %s", exc)
    try:
        ensure_policy_collection()
    except Exception as exc:
        log.warning("Qdrant policy collection not ready at startup: %s", exc)
    yield


settings = get_settings()
app = FastAPI(title="Customer Support Agent", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    checks: dict = {}
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {exc}"
    try:
        r = httpx.get(f"{settings.qdrant_url}/readyz", timeout=3.0)
        checks["qdrant"] = "ok" if r.status_code == 200 else f"status {r.status_code}"
    except Exception as exc:
        checks["qdrant"] = f"error: {exc}"
    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return HealthResponse(status=overall, checks=checks)


def _load_history(db: Session, session_id: str, limit: int = 30) -> list[dict]:
    rows = (
        db.query(Message)
        .filter(Message.session_id == session_id)
        .order_by(Message.id.desc())
        .limit(limit)
        .all()
    )
    return [{"role": m.role, "content": m.content} for m in reversed(rows)]


def _user_sentiment_history(db: Session, session_id: str) -> list[str]:
    rows = (
        db.query(Message.sentiment)
        .filter(Message.session_id == session_id, Message.role == "user")
        .order_by(Message.id.asc())
        .all()
    )
    return [r[0] or "neutral" for r in rows]


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, db: Session = Depends(get_db)) -> ChatResponse:
    chat_session = db.get(ChatSession, req.session_id)
    if chat_session is None:
        chat_session = ChatSession(id=req.session_id, customer_id=req.customer_id)
        db.add(chat_session)
        db.flush()
    elif req.customer_id and chat_session.customer_id != req.customer_id:
        chat_session.customer_id = req.customer_id

    user_sent = sentiment.detect_sentiment(req.message)
    user_intent = sentiment.detect_intent(req.message)
    cumulative = sentiment.cumulative_sentiment(_user_sentiment_history(db, req.session_id) + [user_sent])

    history = _load_history(db, req.session_id)

    # ── Identification flow ───────────────────────────────────────────────────
    # Bypass the ID gate for persistently frustrated customers with no order context —
    # they need empathy, not another "what's your email?" prompt.
    _skip_id_for_frustration = (
        cumulative == "frustrated"
        and not chat_session.customer_id
        and not any(m.get("role") == "assistant" and
                    any(w in (m.get("content") or "").lower()
                        for w in ["confirmed", "verified", "welcome"])
                    for m in history)
    )
    _customer_id_before = chat_session.customer_id
    id_response = None if _skip_id_for_frustration else identification.handle_identification(
        message=req.message,
        chat_session=chat_session,
        history=history,
        db=db,
    )
    if id_response is not None:
        customer_just_verified = bool(chat_session.customer_id and not _customer_id_before)
        reply_text = id_response
        invocations: list = []
        id_rag_sources: list = []

        # Issue 5: if verification just succeeded AND the message has a real query
        # beyond bare credentials, run the agent so the reply covers both in one turn.
        if customer_just_verified:
            _cred_stripped = re.sub(
                r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'
                r'|\bORD-[\w-]+\b'
                r'|(?:my\s+)?e-?mail(?:\s+address)?(?:\s+is)?\s*'
                r'|(?:my\s+)?order(?:\s+(?:number|#|id))?\s+(?:is\s+)?'
                r'|\bmy\s+name\s+is\b',
                '',
                req.message,
                flags=re.IGNORECASE,
            )
            _cred_stripped = re.sub(r'[\s\-/,.:;!?]+', ' ', _cred_stripped).strip()
            if _cred_stripped:
                ctx_id = tools.ToolContext(
                    db=db,
                    session_id=req.session_id,
                    customer_id=chat_session.customer_id,
                    customer_email=None,
                )
                try:
                    agent_reply, invocations = agent.run_agent(
                        user_message=req.message,
                        history=history,
                        sentiment=user_sent,
                        cumulative=cumulative,
                        ctx=ctx_id,
                    )
                    reply_text = id_response + "\n\n" + agent_reply
                    id_rag_sources = [
                        RagSource(asin=h.asin, title=h.title, score=h.score,
                                  category=h.category, price=h.price)
                        for h in ctx_id.rag_hits
                    ]
                except Exception:
                    log.exception("Agent failure after identification")

        db.add(Message(session_id=req.session_id, role="user", content=req.message,
                       sentiment=user_sent, intent=user_intent))
        db.add(Message(session_id=req.session_id, role="assistant", content=reply_text,
                       tools_called=[
                           {"name": inv.name, "arguments": inv.arguments, "result": inv.result}
                           for inv in invocations
                       ]))
        db.commit()
        return ChatResponse(
            reply=reply_text,
            sentiment=user_sent,
            intent=user_intent,
            escalated=False,
            customer_verified=customer_just_verified,
            tools_called=invocations,
            rag_sources=id_rag_sources,
            session_id=req.session_id,
        )

    ctx = tools.ToolContext(
        db=db,
        session_id=req.session_id,
        customer_id=chat_session.customer_id,
        customer_email=None,
    )

    try:
        reply_text, invocations = agent.run_agent(
            user_message=req.message,
            history=history,
            sentiment=user_sent,
            cumulative=cumulative,
            ctx=ctx,
        )
    except Exception as exc:
        log.exception("Agent failure")
        raise HTTPException(status_code=502, detail=f"LLM error: {exc}")

    escalated = sentiment.should_escalate(user_sent, cumulative, user_intent)
    chat_session.cumulative_sentiment = cumulative
    if escalated:
        chat_session.escalated = True

    db.add(
        Message(
            session_id=req.session_id,
            role="user",
            content=req.message,
            sentiment=user_sent,
            intent=user_intent,
        )
    )
    db.add(
        Message(
            session_id=req.session_id,
            role="assistant",
            content=reply_text,
            sentiment=user_sent,
            intent=user_intent,
            tools_called=[
                {"name": inv.name, "arguments": inv.arguments, "result": inv.result}
                for inv in invocations
            ],
        )
    )

    rag_sources = [
        RagSource(
            asin=h.asin,
            title=h.title,
            score=h.score,
            category=h.category,
            price=h.price,
        )
        for h in ctx.rag_hits
    ]

    return ChatResponse(
        reply=reply_text,
        sentiment=user_sent,
        intent=user_intent,
        escalated=escalated,
        tools_called=invocations,
        rag_sources=rag_sources,
        session_id=req.session_id,
    )


# ── Direct catalog search (for the UI Catalog Explorer) ────────────
@app.get("/rag/stats")
def rag_stats() -> dict:
    return rag_mod.collection_stats()


@app.get("/rag/search")
def rag_search(q: str, top_k: int = 6) -> dict:
    q = (q or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="q required")
    top_k = max(1, min(int(top_k), 24))
    try:
        hits = rag_mod.search_products(query=q, top_k=top_k)
    except Exception as exc:
        log.warning("rag/search failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))
    return {
        "query": q,
        "count": len(hits),
        "results": [
            {
                "asin": h.asin,
                "title": h.title,
                "category": h.category,
                "price": h.price,
                "stars": h.stars,
                "url": h.url,
                "score": round(h.score, 4),
            }
            for h in hits
        ],
    }


# ── Direct policy KB search ────────────────────────────────────────
@app.get("/policy/search")
def policy_search(q: str, top_k: int = 4) -> dict:
    q = (q or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="q required")
    top_k = max(1, min(int(top_k), 12))
    try:
        hits = rag_mod.search_policies(query=q, top_k=top_k)
    except Exception as exc:
        log.warning("policy/search failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))
    return {
        "query": q,
        "count": len(hits),
        "results": [
            {
                "topic": h.topic,
                "section": h.section,
                "source": h.source,
                "text": h.text,
                "score": round(h.score, 4),
            }
            for h in hits
        ],
    }


# ── Email inbound webhook (optional) ───────────────────────────────
@app.post("/webhooks/email/inbound")
def inbound_email(payload: dict, db: Session = Depends(get_db)):
    """Accept a parsed inbound email and route it through the same chat pipeline."""
    sender = payload.get("from") or payload.get("sender") or ""
    text_body = payload.get("text") or payload.get("body") or ""
    subject = payload.get("subject") or ""
    if not sender or not text_body:
        raise HTTPException(status_code=400, detail="from + text required")
    session_id = f"email:{sender.lower()}"
    composed = f"[Subject: {subject}]\n\n{text_body}" if subject else text_body
    return chat(
        ChatRequest(message=composed, session_id=session_id, customer_email=sender),
        db=db,
    )


# ── Static UI ──────────────────────────────────────────────────────
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(str(WEB_DIR / "index.html"))
