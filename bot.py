import os, re, logging
import requests
from pathlib import Path
from datetime import datetime, date
from typing import Optional
from html import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from dotenv import load_dotenv

import data_manager as dm
import analyst
import scheduler as sched

load_dotenv(override=True)

# ── CONFIG ────────────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OLLAMA_URL     = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL   = os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b")
ALLOWED_USERS  = os.getenv("ALLOWED_USERS", "")
logger.info(f"ALLOWED_USERS carregado: {repr(ALLOWED_USERS)}")
MAX_HISTORY    = 20
SYSTEM_PROMPT  = os.getenv("SYSTEM_PROMPT",
    "Voce e um assistente pessoal inteligente. Responda sempre em portugues, "
    "de forma clara e amigavel.")

analyst.OLLAMA_URL   = OLLAMA_URL
analyst.OLLAMA_MODEL = OLLAMA_MODEL
sched.OLLAMA_URL     = OLLAMA_URL
sched.OLLAMA_MODEL   = OLLAMA_MODEL

conversation_history: dict[int, list[dict]] = {}
_app = None

# ── UTILS ─────────────────────────────────────────────────────────────────────
def is_allowed(uid: int) -> bool:
    raw = ALLOWED_USERS.strip()
    if not raw: return True
    allowed = [u.strip() for u in re.split(r"[,\s]+", raw) if u.strip()]
    logger.debug(f"is_allowed: uid={uid}, lista={allowed}")
    return str(uid) in allowed

def h(text: str) -> str:
    """Escapa texto para uso seguro dentro de HTML do Telegram."""
    return escape(str(text))

async def send_html(update: Update, text: str):
    """Envia mensagem HTML, fazendo fallback para texto puro se falhar."""
    for i in range(0, max(len(text), 1), 4096):
        chunk = text[i:i+4096]
        try:
            await update.message.reply_text(chunk, parse_mode="HTML")
        except Exception:
            # Remove tags HTML e envia como texto puro
            clean = re.sub(r'<[^>]+>', '', chunk)
            await update.message.reply_text(clean)

async def send_proactive(user_id: int, text: str):
    if _app:
        try:
            await _app.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Erro mensagem proativa: {e}")


# ── HELPERS DE DATA ──────────────────────────────────────────────────────────




# ── HELPERS DE DATA ──────────────────────────────────────────────────────────
MESES_MAP = {
    "janeiro":1,"fevereiro":2,"marco":3,"março":3,"abril":4,"maio":5,"junho":6,
    "julho":7,"agosto":8,"setembro":9,"outubro":10,"novembro":11,"dezembro":12
}

def parse_mes_ano(texto: str):
    """Extrai (mes, ano) de texto como 'novembro de 2025', '11/2025', 'novembro'."""
    t = texto.lower()
    m = re.search(r'(\d{1,2})[/-](\d{4})', t)
    if m: return int(m.group(1)), int(m.group(2))
    m = re.search(r'(\d{4})[/-](\d{1,2})', t)
    if m: return int(m.group(2)), int(m.group(1))
    for nome, num in MESES_MAP.items():
        if nome in t:
            ano_m = re.search(r'\d{4}', t)
            return num, int(ano_m.group()) if ano_m else datetime.now().year
    return None, None


# ── ROTEADOR ──────────────────────────────────────────────────────────────────
def decide_tool(msg: str, history: list[dict]) -> dict:
    ml = msg.lower()

    if any(w in ml for w in ["meus agendamentos", "listar agenda", "ver agenda", "ver agendamentos"]):
        return {"tool": "schedule_list"}
    if any(w in ml for w in ["excluir agenda", "deletar agenda", "remover agenda",
                              "excluir agendamento", "deletar agendamento", "remover agendamento", "cancelar agendamento"]):
        return {"tool": "schedule_delete"}
    if any(w in ml for w in ["todo dia", "toda semana", "toda segunda", "toda terca", "toda quarta",
                              "toda quinta", "toda sexta", "todo sabado", "todo domingo", "todo mes",
                              "me mande", "me envie", "me avise", "agende", "criar alerta"]):
        return {"tool": "schedule_create"}
    if any(w in ml for w in ["importar", "importa"]) and \
       any(w in ml for w in [".csv", ".xlsx", ".xls", "planilha", "extrato"]):
        return {"tool": "import_sheet"}
    meses_pt = ["janeiro","fevereiro","marco","março","abril","maio","junho",
                 "julho","agosto","setembro","outubro","novembro","dezembro"]
    pergunta_financeira = any(w in ml for w in [
        "quanto gastei", "quanto gasto", "total gasto", "relatorio",
        "analise", "analisa", "resumo dos gastos", "gastos do mes",
        "gastos da semana", "por categoria", "orcamento", "media de gasto",
        "ganhos", "ganho", "receita", "receitas", "salario", "salário",
        "entradas", "quanto recebi", "quanto ganhei", "renda",
        "fluxo de caixa", "saldo", "sobrou", "faltou", "periodo",
        "gasto total", "total de gastos", "total de despesas",
        "gastou", "gasto em", "gastos em", "gastos de"
    ])
    # Qualquer pergunta que mencione um mês vai para finance_query
    menciona_mes = any(m in ml for m in meses_pt)
    if pergunta_financeira or menciona_mes:
        return {"tool": "finance_query"}
    if any(w in ml for w in ["gastei", "paguei", "comprei", "almocei", "jantei",
                              "ifood", "rappi", "gasolina", "abasteci", "farmacia",
                              "mercado", "supermercado", "academia", "aluguel", "boleto"]):
        return {"tool": "expense"}
    if any(w in ml for w in ["gastei", "paguei", "comprei"]):
        return {"tool": "expense"}
    return {"tool": "chat"}

# ── CHAT COM OLLAMA ───────────────────────────────────────────────────────────
def chat_with_ollama(user_id: int, user_message: str) -> str:
    if user_id not in conversation_history:
        conversation_history[user_id] = []
    conversation_history[user_id].append({"role": "user", "content": user_message})
    if len(conversation_history[user_id]) > MAX_HISTORY:
        conversation_history[user_id] = conversation_history[user_id][-MAX_HISTORY:]
    try:
        resp = requests.post(f"{OLLAMA_URL}/api/chat",
            json={"model": OLLAMA_MODEL,
                  "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                *conversation_history[user_id]],
                  "stream": False}, timeout=180)
        resp.raise_for_status()
        answer = resp.json()["message"]["content"]
        conversation_history[user_id].append({"role": "assistant", "content": answer})
        return answer
    except requests.exceptions.ConnectionError:
        return "❌ Ollama offline. Rode: <code>ollama serve</code>"
    except requests.exceptions.Timeout:
        return "⏳ Timeout. Tente uma pergunta mais curta."
    except Exception as e:
        return f"❌ Erro: {h(str(e))}"

# ── HANDLER PRINCIPAL ─────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    uname = update.effective_user.first_name
    text  = update.message.text
    logger.info(f"[{uid}] {uname}: {text!r}")

    if not is_allowed(uid):
        await update.message.reply_text(f"🚫 Acesso negado. Seu ID: <code>{uid}</code>", parse_mode="HTML")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    decision = decide_tool(text, conversation_history.get(uid, []))
    tool     = decision.get("tool", "chat")
    reply    = ""

    # ── Registrar gasto ───────────────────────────────────────────────────────
    if tool == "expense":
        expense = analyst.extract_expense(text)
        if expense:
            eid = dm.add_expense(expense["amount"], expense["category"],
                                 expense.get("desc", ""), expense.get("date"))
            now    = datetime.now()
            alerts = dm.check_budget_alerts(now.year, now.month)
            alert_txt = ""
            for a in alerts:
                if a["category"] == expense["category"]:
                    emoji = "🔴" if a["pct"] >= 100 else "🟡"
                    alert_txt = (f"\n\n{emoji} <b>Atenção:</b> você já usou "
                                 f"<b>{a['pct']}%</b> do limite de "
                                 f"R$ {a['limit']:.2f} em <i>{h(a['category'].title())}</i>.")
            reply = (
                f"✅ <b>Gasto registrado!</b>  <code>#{eid}</code>\n"
                f"💰 <b>R$ {expense['amount']:.2f}</b> — {h(expense['category'].title())}\n"
                f"📝 {h(expense.get('desc', ''))}\n"
                f"📅 {expense.get('date', date.today().isoformat())}"
                f"{alert_txt}"
            )
        else:
            reply = chat_with_ollama(uid, text)

    # ── Consulta financeira ───────────────────────────────────────────────────
    elif tool == "finance_query":
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        reply = analyst.answer_from_sheets(text)

    # ── Criar agendamento ─────────────────────────────────────────────────────
    elif tool == "schedule_create":
        schedule = sched.extract_schedule(text)
        if schedule:
            sid = sched.save_schedule(uid, schedule["name"], schedule["job_type"],
                                      schedule["cron"], schedule.get("params", {}))
            sched.register_job(sid, uid, schedule["job_type"], schedule["cron"], schedule.get("params", {}))
            reply = (
                f"📅 <b>Agendamento criado!</b>\n\n"
                f"📌 {h(schedule['name'])}\n"
                f"🔁 {h(sched.cron_to_human(schedule['cron']))}\n"
                f"🆔 ID: <code>{sid}</code>\n\n"
                f"<i>Diga 'excluir agendamento {sid}' para remover.</i>"
            )
        else:
            reply = (
                "❓ Não entendi o agendamento. Tente:\n\n"
                "<code>Me mande análise mensal todo dia 1 às 8h</code>\n"
                "<code>Resumo semanal toda segunda às 9h</code>\n"
                "<code>Alertas de gasto todo dia às 20h</code>"
            )

    # ── Listar agendamentos ───────────────────────────────────────────────────
    elif tool == "schedule_list":
        schedules = sched.list_schedules(uid)
        if not schedules:
            reply = ("📭 Nenhum agendamento configurado.\n\n"
                     "<i>Exemplo: 'Me mande resumo semanal toda segunda às 9h'</i>")
        else:
            lines = ["📅 <b>Seus agendamentos:</b>\n"]
            for s in schedules:
                icon = "✅" if s["active"] else "⏸️"
                lines.append(
                    f"{icon} <b>{h(s['name'])}</b>  <code>#{s['id']}</code>\n"
                    f"   🔁 {h(sched.cron_to_human(s['cron']))}"
                )
            lines.append("\n<i>Diga 'excluir agendamento &lt;ID&gt;' para remover.</i>")
            reply = "\n".join(lines)

    # ── Excluir agendamento ───────────────────────────────────────────────────
    elif tool == "schedule_delete":
        ids = re.findall(r'\d+', text)
        if ids:
            sid = int(ids[0])
            ok  = sched.delete_schedule_db(sid, uid)
            if ok:
                sched.remove_job(sid)
                reply = f"🗑️ Agendamento <code>#{sid}</code> excluído."
            else:
                reply = f"❌ Agendamento <code>#{sid}</code> não encontrado."
        else:
            schedules = sched.list_schedules(uid)
            if not schedules:
                reply = "📭 Nenhum agendamento para excluir."
            else:
                keyboard = [[InlineKeyboardButton(
                    f"🗑️ {s['name']} (#{s['id']})",
                    callback_data=f"del_sched_{s['id']}"
                )] for s in schedules]
                await update.message.reply_text(
                    "Qual agendamento deseja excluir?",
                    reply_markup=InlineKeyboardMarkup(keyboard))
                return

    # ── Importar planilha ─────────────────────────────────────────────────────
    elif tool == "import_sheet":
        match = re.search(r'[\w\-]+\.(csv|xlsx|xls)', text, re.IGNORECASE)
        if not match:
            reply = ("📂 Qual arquivo deseja importar?\n\n"
                     "<i>Diga: 'importar planilha gastos.csv'</i>\n"
                     "O arquivo deve estar na pasta <code>data/sheets/</code>")
        else:
            filename = match.group(0)
            filepath = Path("./data/sheets") / filename
            if not filepath.exists():
                reply = (f"❌ Arquivo <code>{h(filename)}</code> não encontrado.\n"
                         f"Copie para <code>data/sheets/</code> e tente novamente.")
            else:
                imported, errors = dm.import_sheet(filepath)
                if errors:
                    err_list = "\n".join(f"• {h(e)}" for e in errors[:5])
                    reply = (f"⚠️ <b>{imported}</b> registros importados com "
                             f"<b>{len(errors)}</b> erro(s):\n{err_list}")
                else:
                    reply = (f"✅ <b>Planilha importada!</b>\n"
                             f"📊 <b>{imported}</b> registros de <code>{h(filename)}</code>")

    # ── Chat geral ────────────────────────────────────────────────────────────
    else:
        reply = chat_with_ollama(uid, text)

    await send_html(update, reply)

# ── CALLBACK INLINE ───────────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    data = query.data
    if data.startswith("del_sched_"):
        sid = int(data.split("_")[-1])
        ok  = sched.delete_schedule_db(sid, uid)
        if ok:
            sched.remove_job(sid)
            await query.edit_message_text(f"🗑️ Agendamento #{sid} excluído.")
        else:
            await query.edit_message_text(f"❌ Agendamento #{sid} não encontrado.")





# ── COMANDOS ──────────────────────────────────────────────────────────────────




async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    n_sched = len(sched.list_schedules(update.effective_user.id))
    await send_html(update,
        f"👋 Olá, <b>{h(update.effective_user.first_name)}</b>!\n\n"
        f"<b>Painel de Controle de Gastos</b>\n\n"
        f"🤖 Modelo: <code>LLaMma 3.2</code>\n"
        f"📅 Agendamentos ativos: <b>{n_sched}</b>\n\n"
        "<b>Comandos:</b>\n"
        "/resumo - análise do mês atual\n"
        "/gastos - últimos saídas\n"
        "/ganhos - últimas entradas\n"
        "/limite - limites por categoria\n"
        "/agendamentos - ver agendamentos\n"
        "/importar - importar planilha\n"
        "/status - status do sistema\n"
        "/clear - limpar histórico\n\n" 
    )

async def cmd_gastos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    args_txt = " ".join(context.args) if context.args else ""
    mes, ano = parse_mes_ano(args_txt)

    if mes and ano:
        start = f"{ano}-{mes:02d}-01"
        end   = f"{ano}-{mes:02d}-31"
        df    = dm.get_expenses(start=start, end=end, limit=200)
        titulo = f"Gastos de {args_txt.strip().title()}"
    else:
        df     = dm.get_last_expenses(15)
        titulo = "Últimos gastos"

    if df.empty:
        await send_html(update, "📭 Nenhum gasto registrado nesse período.")
        return
    total = df["amount"].abs().sum()
    lines = [f"💳 <b>{h(titulo)}</b>  |  Total: <b>R$ {total:.2f}</b>\n"]
    for _, r in df.iterrows():
        pay = f" · <i>{h(r['payment'])}</i>" if r.get("payment") else ""
        lines.append(
            f"<code>{r['date']}</code>  <b>R$ {abs(r['amount']):.2f}</b>  "
            f"{h(r['category'].title())}{pay}\n"
            f"   <i>{h(str(r['desc']))}</i>  <code>#{r['id']}</code>"
        )
    await send_html(update, "\n".join(lines))

async def cmd_ganhos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    args_txt = " ".join(context.args) if context.args else ""
    mes, ano = parse_mes_ano(args_txt)

    if mes and ano:
        start  = f"{ano}-{mes:02d}-01"
        end    = f"{ano}-{mes:02d}-31"
        df     = dm.get_last_income(200, start=start, end=end)
        titulo = f"Receitas de {args_txt.strip().title()}"
    else:
        df     = dm.get_last_income(15)
        titulo = "Últimas receitas"

    if df.empty:
        await send_html(update, "📭 Nenhuma receita registrada nesse período.")
        return
    total = df["amount"].sum()
    lines = [f"💚 <b>{h(titulo)}</b>  |  Total: <b>R$ {total:.2f}</b>\n"]
    for _, r in df.iterrows():
        pay = f" · <i>{h(r['payment'])}</i>" if r.get("payment") else ""
        lines.append(
            f"<code>{r['date']}</code>  <b>R$ {r['amount']:.2f}</b>  "
            f"{h(r['category'].title())}{pay}\n"
            f"   <i>{h(str(r['desc']))}</i>  <code>#{r['id']}</code>"
        )
    await send_html(update, "\n".join(lines))

async def cmd_resumo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    await send_html(update, "⏳ <i>Analisando seus gastos do mês...</i>")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    now   = datetime.now()
    reply = analyst.analyze_month(now.year, now.month)
    await send_html(update, reply)

async def cmd_agendamentos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    schedules = sched.list_schedules(update.effective_user.id)
    if not schedules:
        await send_html(update,
            "📭 Nenhum agendamento configurado.\n"
            "<i>Exemplo: 'Me mande análise mensal todo dia 1 às 8h'</i>")
        return
    lines = ["📅 <b>Agendamentos ativos:</b>\n"]
    for s in schedules:
        icon = "✅" if s["active"] else "⏸️"
        lines.append(
            f"{icon} <b>{h(s['name'])}</b>  <code>#{s['id']}</code>\n"
            f"   🔁 {h(sched.cron_to_human(s['cron']))}"
        )
    lines.append("\n<i>Diga 'excluir agendamento &lt;ID&gt;' para remover.</i>")
    await send_html(update, "\n".join(lines))

async def cmd_limite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    args = context.args
    if len(args) < 2:
        budgets = dm.get_budgets()
        if budgets.empty:
            await send_html(update,
                "ℹ️ Uso: /limite &lt;categoria&gt; &lt;valor&gt;\n"
                "<i>Ex: /limite delivery 200</i>")
        else:
            lines = ["💰 <b>Limites configurados:</b>\n"]
            for _, r in budgets.iterrows():
                lines.append(f"• {h(r['category'].title())}: <b>R$ {r['limit_amount']:.2f}</b>")
            await send_html(update, "\n".join(lines))
        return
    try:
        valor = float(args[1].replace(",", "."))
        dm.set_budget(args[0].lower(), valor)
        await send_html(update,
            f"✅ Limite de <b>R$ {valor:.2f}</b> definido para "
            f"<b>{h(args[0].title())}</b>.")
    except ValueError:
        await send_html(update, "❌ Valor inválido. Ex: /limite delivery 200")

async def cmd_importar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    args = context.args
    if args:
        filename = args[0]
        filepath = Path("./data/sheets") / filename
        if not filepath.exists():
            await send_html(update,
                f"❌ Arquivo <code>{h(filename)}</code> não encontrado em "
                f"<code>data/sheets/</code>")
            return
        await send_html(update, f"⏳ Importando <code>{h(filename)}</code>...")
        imported, errors = dm.import_sheet(filepath)
        if errors:
            err_list = "\n".join(f"• {h(e)}" for e in errors[:5])
            await send_html(update,
                f"⚠️ <b>{imported}</b> registros importados, "
                f"<b>{len(errors)}</b> erro(s):\n{err_list}")
        else:
            await send_html(update,
                f"✅ <b>Importado com sucesso!</b>\n"
                f"📊 <b>{imported}</b> registros adicionados.")
    else:
        await send_html(update,
            "📥 <b>Como importar uma planilha:</b>\n\n"
            "1. Coloque o arquivo em <code>data/sheets/</code>\n"
            "2. Use: <code>/importar gastos.csv</code>\n\n"
            "<b>Colunas aceitas:</b>\n"
            "• <code>date</code> ou <code>data</code> — YYYY-MM-DD\n"
            "• <code>amount</code> ou <code>valor</code> — número (negativo = despesa)\n"
            "• <code>category</code> ou <code>categoria</code>\n"
            "• <code>desc</code> — descrição (opcional)\n"
            "• <code>forma_pagamento</code> — opcional"
        )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    try:
        resp   = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        models = [m["name"] for m in resp.json().get("models", [])]
        df_exp = dm.get_expenses(limit=1)
        n_sched = len(sched.list_schedules(update.effective_user.id))
        await send_html(update,
            f"✅ <b>Sistema online</b>\n\n"
            f"🤖 Modelo: <code>{OLLAMA_MODEL}</code>\n"
            f"📦 Instalados: <code>{', '.join(models) or 'nenhum'}</code>\n\n"
            f"💳 Dados no banco: {'✅' if not df_exp.empty else '📭 nenhum ainda'}\n"
            f"📅 Agendamentos: <b>{n_sched}</b>"
        )
    except Exception:
        await send_html(update, "❌ <b>Ollama offline.</b>\nRode: <code>ollama serve</code>")

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    conversation_history.pop(update.effective_user.id, None)
    await send_html(update, "🗑️ Histórico limpo!")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Erro: {context.error}", exc_info=context.error)

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    global _app
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN nao definido no .env!")

    dm.init_db()
    sched.init_schedules_db()

    _app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    sched.register_send_callback(send_proactive)
    sched.load_all_schedules()
    sched.scheduler.start()

    _app.add_handler(CommandHandler("start",        cmd_start))
    _app.add_handler(CommandHandler("gastos",       cmd_gastos))
    _app.add_handler(CommandHandler("ganhos",       cmd_ganhos))
    _app.add_handler(CommandHandler("resumo",       cmd_resumo))
    _app.add_handler(CommandHandler("agendamentos", cmd_agendamentos))
    _app.add_handler(CommandHandler("limite",       cmd_limite))
    _app.add_handler(CommandHandler("importar",     cmd_importar))
    _app.add_handler(CommandHandler("status",       cmd_status))
    _app.add_handler(CommandHandler("clear",        cmd_clear))
    _app.add_handler(CallbackQueryHandler(handle_callback))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    _app.add_error_handler(error_handler)

    logger.info(f"Bot iniciado | modelo: {OLLAMA_MODEL}")
    _app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

# ── COMANDOS ──────────────────────────────────────────────────────────────────




async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    n_sched = len(sched.list_schedules(update.effective_user.id))
    await send_html(update,
        f"👋 Olá, <b>{h(update.effective_user.first_name)}</b>!\n\n"
        f"<b>Painel de Controle de Gastos</b>\n\n"
        f"🤖 Modelo: <code>{OLLAMA_MODEL}</code>\n"
        f"📅 Agendamentos ativos: <b>{n_sched}</b>\n\n"
        "<b>Comandos:</b>\n"
        "/resumo — análise do mês atual\n"
        "/gastos [mês ano] — gastos (ex: /gastos novembro 2025)\n"
        "/ganhos [mês ano] — receitas (ex: /ganhos janeiro 2026)\n"
        "/limite — limites mensais por categoria\n"
        "/agendamentos — ver agendamentos\n"
        "/importar — importar planilha\n"
        "/status — status do sistema\n"
        "/clear — limpar histórico\n\n"
        "<b>Linguagem natural:</b>\n"
        "<i>Quanto gastei em novembro de 2025?</i>\n"
        "<i>Qual meu saldo em janeiro de 2026?</i>\n"
        "<i>Gastei 45 reais no iFood</i>"
    )

async def cmd_gastos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    args_txt = " ".join(context.args) if context.args else ""
    mes, ano = parse_mes_ano(args_txt)

    if mes and ano:
        start = f"{ano}-{mes:02d}-01"
        end   = f"{ano}-{mes:02d}-31"
        df    = dm.get_expenses(start=start, end=end, limit=200)
        titulo = f"Gastos de {args_txt.strip().title()}"
    else:
        df     = dm.get_last_expenses(15)
        titulo = "Últimos gastos"

    if df.empty:
        await send_html(update, "📭 Nenhum gasto registrado nesse período.")
        return
    total = df["amount"].abs().sum()
    lines = [f"💳 <b>{h(titulo)}</b>  |  Total: <b>R$ {total:.2f}</b>\n"]
    for _, r in df.iterrows():
        pay = f" · <i>{h(r['payment'])}</i>" if r.get("payment") else ""
        lines.append(
            f"<code>{r['date']}</code>  <b>R$ {abs(r['amount']):.2f}</b>  "
            f"{h(r['category'].title())}{pay}\n"
            f"   <i>{h(str(r['desc']))}</i>  <code>#{r['id']}</code>"
        )
    await send_html(update, "\n".join(lines))

async def cmd_ganhos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    args_txt = " ".join(context.args) if context.args else ""
    mes, ano = parse_mes_ano(args_txt)

    if mes and ano:
        start  = f"{ano}-{mes:02d}-01"
        end    = f"{ano}-{mes:02d}-31"
        df     = dm.get_last_income(200, start=start, end=end)
        titulo = f"Receitas de {args_txt.strip().title()}"
    else:
        df     = dm.get_last_income(15)
        titulo = "Últimas receitas"

    if df.empty:
        await send_html(update, "📭 Nenhuma receita registrada nesse período.")
        return
    total = df["amount"].sum()
    lines = [f"💚 <b>{h(titulo)}</b>  |  Total: <b>R$ {total:.2f}</b>\n"]
    for _, r in df.iterrows():
        pay = f" · <i>{h(r['payment'])}</i>" if r.get("payment") else ""
        lines.append(
            f"<code>{r['date']}</code>  <b>R$ {r['amount']:.2f}</b>  "
            f"{h(r['category'].title())}{pay}\n"
            f"   <i>{h(str(r['desc']))}</i>  <code>#{r['id']}</code>"
        )
    await send_html(update, "\n".join(lines))

async def cmd_resumo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    await send_html(update, "⏳ <i>Analisando seus gastos do mês...</i>")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    now   = datetime.now()
    reply = analyst.analyze_month(now.year, now.month)
    await send_html(update, reply)

async def cmd_agendamentos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    schedules = sched.list_schedules(update.effective_user.id)
    if not schedules:
        await send_html(update,
            "📭 Nenhum agendamento configurado.\n"
            "<i>Exemplo: 'Me mande análise mensal todo dia 1 às 8h'</i>")
        return
    lines = ["📅 <b>Agendamentos ativos:</b>\n"]
    for s in schedules:
        icon = "✅" if s["active"] else "⏸️"
        lines.append(
            f"{icon} <b>{h(s['name'])}</b>  <code>#{s['id']}</code>\n"
            f"   🔁 {h(sched.cron_to_human(s['cron']))}"
        )
    lines.append("\n<i>Diga 'excluir agendamento &lt;ID&gt;' para remover.</i>")
    await send_html(update, "\n".join(lines))

async def cmd_limite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    args = context.args
    if len(args) < 2:
        budgets = dm.get_budgets()
        if budgets.empty:
            await send_html(update,
                "ℹ️ Uso: /limite &lt;categoria&gt; &lt;valor&gt;\n"
                "<i>Ex: /limite delivery 200</i>")
        else:
            lines = ["💰 <b>Limites configurados:</b>\n"]
            for _, r in budgets.iterrows():
                lines.append(f"• {h(r['category'].title())}: <b>R$ {r['limit_amount']:.2f}</b>")
            await send_html(update, "\n".join(lines))
        return
    try:
        valor = float(args[1].replace(",", "."))
        dm.set_budget(args[0].lower(), valor)
        await send_html(update,
            f"✅ Limite de <b>R$ {valor:.2f}</b> definido para "
            f"<b>{h(args[0].title())}</b>.")
    except ValueError:
        await send_html(update, "❌ Valor inválido. Ex: /limite delivery 200")

async def cmd_importar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    args = context.args
    if args:
        filename = args[0]
        filepath = Path("./data/sheets") / filename
        if not filepath.exists():
            await send_html(update,
                f"❌ Arquivo <code>{h(filename)}</code> não encontrado em "
                f"<code>data/sheets/</code>")
            return
        await send_html(update, f"⏳ Importando <code>{h(filename)}</code>...")
        imported, errors = dm.import_sheet(filepath)
        if errors:
            err_list = "\n".join(f"• {h(e)}" for e in errors[:5])
            await send_html(update,
                f"⚠️ <b>{imported}</b> registros importados, "
                f"<b>{len(errors)}</b> erro(s):\n{err_list}")
        else:
            await send_html(update,
                f"✅ <b>Importado com sucesso!</b>\n"
                f"📊 <b>{imported}</b> registros adicionados.")
    else:
        await send_html(update,
            "📥 <b>Como importar uma planilha:</b>\n\n"
            "1. Coloque o arquivo em <code>data/sheets/</code>\n"
            "2. Use: <code>/importar gastos.csv</code>\n\n"
            "<b>Colunas aceitas:</b>\n"
            "• <code>date</code> ou <code>data</code> — YYYY-MM-DD\n"
            "• <code>amount</code> ou <code>valor</code> — número (negativo = despesa)\n"
            "• <code>category</code> ou <code>categoria</code>\n"
            "• <code>desc</code> — descrição (opcional)\n"
            "• <code>forma_pagamento</code> — opcional"
        )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    try:
        resp   = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        models = [m["name"] for m in resp.json().get("models", [])]
        df_exp = dm.get_expenses(limit=1)
        n_sched = len(sched.list_schedules(update.effective_user.id))
        await send_html(update,
            f"✅ <b>Sistema online</b>\n\n"
            f"🤖 Modelo: <code>{OLLAMA_MODEL}</code>\n"
            f"📦 Instalados: <code>{', '.join(models) or 'nenhum'}</code>\n\n"
            f"💳 Dados no banco: {'✅' if not df_exp.empty else '📭 nenhum ainda'}\n"
            f"📅 Agendamentos: <b>{n_sched}</b>"
        )
    except Exception:
        await send_html(update, "❌ <b>Ollama offline.</b>\nRode: <code>ollama serve</code>")

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    conversation_history.pop(update.effective_user.id, None)
    await send_html(update, "🗑️ Histórico limpo!")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Erro: {context.error}", exc_info=context.error)

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    global _app
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN nao definido no .env!")

    dm.init_db()
    sched.init_schedules_db()

    _app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    sched.register_send_callback(send_proactive)
    sched.load_all_schedules()
    sched.scheduler.start()

    _app.add_handler(CommandHandler("start",        cmd_start))
    _app.add_handler(CommandHandler("gastos",       cmd_gastos))
    _app.add_handler(CommandHandler("ganhos",       cmd_ganhos))
    _app.add_handler(CommandHandler("resumo",       cmd_resumo))
    _app.add_handler(CommandHandler("agendamentos", cmd_agendamentos))
    _app.add_handler(CommandHandler("limite",       cmd_limite))
    _app.add_handler(CommandHandler("importar",     cmd_importar))
    _app.add_handler(CommandHandler("status",       cmd_status))
    _app.add_handler(CommandHandler("clear",        cmd_clear))
    _app.add_handler(CallbackQueryHandler(handle_callback))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    _app.add_error_handler(error_handler)

    logger.info(f"Bot iniciado | modelo: {OLLAMA_MODEL}")
    _app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()