"""
scheduler.py
Agendamentos persistentes em SQLite.
Criados, editados e excluídos via conversa no Telegram.
"""

import json
import re
import sqlite3
import logging
import requests
from pathlib import Path
from datetime import datetime, date
from typing import Optional, Callable, Awaitable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

DB_PATH    = Path("./data/expenses.db")   # reutiliza o mesmo banco
OLLAMA_URL   = None
OLLAMA_MODEL = None

scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")

# Callback registrado pelo bot para enviar mensagens proativas
_send_callback: Optional[Callable[[int, str], Awaitable[None]]] = None


def register_send_callback(fn: Callable[[int, str], Awaitable[None]]):
    global _send_callback
    _send_callback = fn


# ─── PERSISTÊNCIA ─────────────────────────────────────────────────────────────

def init_schedules_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                name        TEXT NOT NULL,
                job_type    TEXT NOT NULL,
                cron        TEXT NOT NULL,
                params      TEXT DEFAULT '{}',
                active      INTEGER DEFAULT 1,
                created     TEXT DEFAULT (datetime('now'))
            )
        """)
        con.commit()


def save_schedule(user_id: int, name: str, job_type: str,
                  cron: str, params: dict = {}) -> int:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "INSERT INTO schedules (user_id, name, job_type, cron, params) VALUES (?,?,?,?,?)",
            (user_id, name, job_type, cron, json.dumps(params, ensure_ascii=False))
        )
        con.commit()
        return cur.lastrowid


def list_schedules(user_id: int) -> list[dict]:
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT id, name, job_type, cron, params, active FROM schedules WHERE user_id=?",
            (user_id,)
        ).fetchall()
    return [
        {"id": r[0], "name": r[1], "job_type": r[2],
         "cron": r[3], "params": json.loads(r[4]), "active": bool(r[5])}
        for r in rows
    ]


def delete_schedule_db(schedule_id: int, user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "DELETE FROM schedules WHERE id=? AND user_id=?", (schedule_id, user_id)
        )
        con.commit()
        return cur.rowcount > 0


def toggle_schedule_db(schedule_id: int, user_id: int, active: bool) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "UPDATE schedules SET active=? WHERE id=? AND user_id=?",
            (1 if active else 0, schedule_id, user_id)
        )
        con.commit()
        return cur.rowcount > 0


# ─── EXTRAÇÃO DE AGENDAMENTO EM LINGUAGEM NATURAL ────────────────────────────

SCHEDULE_EXTRACT_PROMPT = """Extraia as informações de agendamento da mensagem abaixo.
Responda SOMENTE com JSON válido.

Tipos de job disponíveis:
- "monthly_analysis": análise mensal de gastos
- "weekly_digest": resumo semanal de gastos
- "budget_alert": verificação de alertas de limite
- "custom_report": relatório personalizado

Exemplos de cron:
- "todo dia às 8h" → "0 8 * * *"
- "toda segunda às 9h" → "0 9 * * 1"
- "todo dia 1 às 8h" → "0 8 1 * *"
- "toda sexta às 18h" → "0 18 * * 5"
- "todo domingo às 20h" → "0 20 * * 0"

Mensagem: "{msg}"

JSON:
{{
  "name": "<nome descritivo curto>",
  "job_type": "<tipo>",
  "cron": "<expressão cron>",
  "params": {{}}
}}

Se não for um pedido de agendamento, retorne: {{"job_type": null}}"""


def extract_schedule(message: str) -> Optional[dict]:
    prompt = SCHEDULE_EXTRACT_PROMPT.format(msg=message)
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={"model": OLLAMA_MODEL,
                  "messages": [{"role": "user", "content": prompt}],
                  "stream": False},
            timeout=30
        )
        raw   = resp.json()["message"]["content"].strip()
        match = re.search(r'\{.*?\}', raw, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group())
        return None if data.get("job_type") is None else data
    except Exception as e:
        logger.warning(f"extract_schedule falhou: {e}")
        return None


# ─── EXECUÇÃO DOS JOBS ────────────────────────────────────────────────────────

async def _run_job(user_id: int, job_type: str, params: dict):
    if not _send_callback:
        logger.warning("send_callback não registrado")
        return

    # Import local para evitar circular
    import analyst

    try:
        if job_type == "monthly_analysis":
            now = datetime.now()
            text = analyst.analyze_month(now.year, now.month)
            await _send_callback(user_id, f"📊 *Análise mensal automática:*\n\n{text}")

        elif job_type == "weekly_digest":
            text = analyst.generate_weekly_digest()
            await _send_callback(user_id, f"📅 *Resumo semanal:*\n\n{text}")

        elif job_type == "budget_alert":
            now  = datetime.now()
            text = analyst.check_and_format_alerts(now.year, now.month)
            if text:
                await _send_callback(user_id, text)
            else:
                await _send_callback(user_id, "✅ Todos os gastos dentro dos limites configurados.")

        elif job_type == "custom_report":
            question = params.get("question", "Faça um resumo dos meus gastos recentes.")
            text = analyst.answer_from_sheets(question)
            await _send_callback(user_id, f"📋 *Relatório:*\n\n{text}")

    except Exception as e:
        logger.error(f"Erro no job {job_type}: {e}")
        await _send_callback(user_id, f"❌ Erro ao executar '{job_type}': {e}")


# ─── GERENCIAMENTO DO SCHEDULER ───────────────────────────────────────────────

def _make_job_id(schedule_id: int) -> str:
    return f"schedule_{schedule_id}"


def register_job(schedule_id: int, user_id: int, job_type: str, cron: str, params: dict):
    """Adiciona job ao APScheduler em memória."""
    try:
        parts = cron.strip().split()
        if len(parts) != 5:
            raise ValueError(f"Cron inválido: {cron}")
        minute, hour, day, month, dow = parts
        trigger = CronTrigger(
            minute=minute, hour=hour, day=day, month=month, day_of_week=dow,
            timezone="America/Sao_Paulo"
        )
        scheduler.add_job(
            _run_job,
            trigger=trigger,
            args=[user_id, job_type, params],
            id=_make_job_id(schedule_id),
            replace_existing=True
        )
        logger.info(f"Job registrado: id={schedule_id} type={job_type} cron={cron}")
    except Exception as e:
        logger.error(f"Erro ao registrar job {schedule_id}: {e}")


def remove_job(schedule_id: int):
    job_id = _make_job_id(schedule_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def load_all_schedules():
    """Carrega todos os agendamentos salvos no banco ao iniciar."""
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT id, user_id, job_type, cron, params FROM schedules WHERE active=1"
        ).fetchall()
    for r in rows:
        schedule_id, user_id, job_type, cron, params_json = r
        register_job(schedule_id, user_id, job_type, cron, json.loads(params_json))
    logger.info(f"✅ {len(rows)} agendamento(s) carregado(s)")


def cron_to_human(cron: str) -> str:
    """Converte cron para descrição legível."""
    try:
        parts = cron.split()
        if len(parts) != 5:
            return cron
        minute, hour, day, month, dow = parts
        days_map = {"0":"dom","1":"seg","2":"ter","3":"qua","4":"qui","5":"sex","6":"sáb","*":"todos os dias"}
        if dow != "*":
            return f"toda {days_map.get(dow, dow)} às {hour}:{minute.zfill(2)}h"
        if day != "*":
            return f"todo dia {day} do mês às {hour}:{minute.zfill(2)}h"
        return f"todo dia às {hour}:{minute.zfill(2)}h"
    except Exception:
        return cron