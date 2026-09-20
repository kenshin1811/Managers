"""The floor manager's console.

The browser turns speech into words; the words come here. Parsing on the
server rather than in the page means the same grammar is reachable from a
microphone, a keyboard, a phone or a script, and that it can be tested without
a browser at all.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import notifier_dep, now_dep, settings_dep
from app.config import Settings
from app.db import get_session
from app.engine import commands
from app.engine.journal import record_decision
from app.models.enums import DecisionAction

router = APIRouter(tags=["console"], prefix="/api/console")


class Spoken(BaseModel):
    transcript: str = Field(..., description="What the manager said, or typed")
    source: str = Field(default="voice", description="voice or typed")


class Spoke(BaseModel):
    ok: bool
    kind: str
    speech: str
    replanned: bool
    detail: dict[str, Any] = {}


@router.get("/vocabulary")
def vocabulary(session: Session = Depends(get_session)) -> dict[str, Any]:
    """What the console will recognise.

    The page shows these as hints so nobody has to guess the wording, and the
    list comes from the database rather than a hard-coded copy -- add a
    product and the console understands it immediately.
    """
    vocab = commands.Vocabulary.load(session)
    return {
        "products": [{"code": p.code, "name": p.name, "kind": p.kind} for p in vocab.products],
        "destinations": [
            {"code": d.code, "name": d.name, "suburb": d.suburb, "run": d.run}
            for d in vocab.destinations
        ],
        "people": [e.full_name for e in vocab.employees if not e.is_manager],
        "examples": [
            "add 60 jam donuts for Fitzroy",
            "two hundred blueberry muffins for South Yarra",
            "Shaleen is off sick",
            "Valentino is back",
            "mark 120 chocolate rings packed",
            "what's at risk",
            "re-plan",
        ],
    }


@router.post("/command", response_model=Spoke)
def command(
    payload: Spoken,
    session: Session = Depends(get_session),
    settings: Settings = Depends(settings_dep),
    notifier=Depends(notifier_dep),
    now: datetime = Depends(now_dep),
) -> Spoke:
    """Run one instruction and say what happened.

    Every instruction is written to the same audit log as the agent's own
    decisions, with the manager as the actor and the words they used. When
    somebody asks later why three hundred jam donuts appeared on the six
    o'clock run, the answer is a row, not a memory.
    """
    result = commands.handle(session, settings, now, payload.transcript, notifier)

    record_decision(
        session,
        now=now,
        action=DecisionAction.OVERRIDDEN if result.ok else DecisionAction.NO_ACTION_NEEDED,
        trigger=f"console_{payload.source}",
        summary=f'Floor manager said "{payload.transcript.strip()}" — {result.speech}',
        subject_type="console",
        actor="manager:console",
        details={
            "transcript": payload.transcript,
            "source": payload.source,
            "understood_as": result.kind,
            "ok": result.ok,
        },
    )
    session.commit()

    return Spoke(
        ok=result.ok,
        kind=result.kind,
        speech=result.speech,
        replanned=result.replanned,
        detail=result.detail,
    )
