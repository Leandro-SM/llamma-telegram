"""
analyst.py
Analisa dados financeiros combinando Pandas com o LLM local.
"""

import json
import re
import requests
import pandas as pd
from datetime import datetime, date
from typing import Optional

import data_manager as dm

OLLAMA_URL   = None
OLLAMA_MODEL = None


def _llm(prompt: str, system: str = "", timeout: int = 180) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
            timeout=timeout
        )
        return resp.json()["message"]["content"].strip()
    except Exception as e:
        return f"Erro LLM: {e}"


# ─── SYSTEM PROMPT DO ANALISTA ────────────────────────────────────────────────

ANALYSIS_SYSTEM = """Você é um analista financeiro pessoal experiente e direto.

FORMATAÇÃO — use sempre HTML do Telegram:
- <b>negrito</b> para valores e totais importantes
- <i>itálico</i> para observações e dicas
- <code>código</code> para categorias e datas
- Emojis no início de seções: 📊 💰 ⚠️ 💡 ✅ 🔴 🟡 🟢
- Separe seções com uma linha em branco

REGRAS DE CONTEÚDO:
- Responda SEMPRE em português brasileiro
- Cite valores reais dos dados com <b>R$ X,XX</b>
- Diferencie receita de despesa claramente
- Calcule e mostre taxa de poupança quando houver receita
- Máximo 3 sugestões práticas baseadas nos dados reais
- Se os gastos estão ruins, diga claramente
- Nunca invente dados que não estejam no contexto"""


# ─── EXTRAÇÃO DE GASTO ────────────────────────────────────────────────────────

EXTRACT_PROMPT = """Extraia os dados de gasto da mensagem. Responda SOMENTE com JSON, sem texto extra.

Categorias: alimentacao, transporte, moradia, saude, lazer, streaming, roupas,
            educacao, mercado, delivery, academia, combustivel, outros

Mensagem: "{msg}"
Data atual: {today}

JSON:
{{
  "amount": <float positivo>,
  "category": "<categoria>",
  "desc": "<descricao curta>",
  "date": "<YYYY-MM-DD ou null>"
}}

Se NAO for registro de gasto: {{"amount": null}}"""


def extract_expense(message: str) -> Optional[dict]:
    prompt = EXTRACT_PROMPT.format(msg=message, today=date.today().isoformat())
    raw    = _llm(prompt)
    try:
        match = re.search(r'\{.*?\}', raw, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group())
        if data.get("amount") is None:
            return None
        data["date"] = data.get("date") or date.today().isoformat()
        return data
    except Exception:
        return None


# ─── ANÁLISE MENSAL ───────────────────────────────────────────────────────────

def _build_monthly_context(year: int, month: int) -> str:
    """Monta contexto rico para o LLM analisar."""
    # Despesas
    expenses = dm.get_expenses(
        start=f"{year}-{month:02d}-01",
        end=f"{year}-{month:02d}-31"
    )
    # Receitas
    income = dm.get_monthly_income(year, month)
    # Resumo por categoria
    summary = dm.get_monthly_summary(year, month)
    # Alertas
    alerts  = dm.check_budget_alerts(year, month)

    total_exp = expenses["amount"].sum() if not expenses.empty else 0
    saldo     = income - total_exp
    taxa_poup = (saldo / income * 100) if income > 0 else None

    lines = [f"=== DADOS FINANCEIROS — {month:02d}/{year} ===\n"]

    if income > 0:
        lines.append(f"RECEITA TOTAL: R$ {income:.2f}")
    lines.append(f"DESPESAS TOTAIS: R$ {total_exp:.2f}")
    if income > 0:
        lines.append(f"SALDO: R$ {saldo:.2f} ({'positivo' if saldo >= 0 else 'negativo'})")
        if taxa_poup is not None:
            lines.append(f"TAXA DE POUPANÇA: {taxa_poup:.1f}%")

    if not summary.empty:
        lines.append("\nDESPESAS POR CATEGORIA (maior para menor):")
        for _, r in summary.iterrows():
            pct = (r["total"] / total_exp * 100) if total_exp > 0 else 0
            lines.append(f"  {r['category'].title():20s} R$ {r['total']:8.2f}  ({pct:.1f}%)  [{int(r['transacoes'])} transações]")

    if not expenses.empty:
        lines.append(f"\nTRANSAÇÕES ({min(len(expenses), 30)} mais recentes):")
        for _, r in expenses.head(30).iterrows():
            pay = f" [{r['payment']}]" if r.get('payment') else ""
            lines.append(f"  {r['date']}  {r['category'].title():15s}  R$ {r['amount']:8.2f}  {r['desc']}{pay}")

    if alerts:
        lines.append("\nALERTAS DE LIMITE:")
        for a in alerts:
            lines.append(f"  {a['category'].title()}: {a['pct']}% do limite de R$ {a['limit']:.2f}")

    return "\n".join(lines)


def analyze_month(year: int, month: int) -> str:
    expenses = dm.get_expenses(
        start=f"{year}-{month:02d}-01",
        end=f"{year}-{month:02d}-31"
    )
    income = dm.get_monthly_income(year, month)

    if expenses.empty and income == 0:
        return f"Nenhum dado registrado em {month:02d}/{year}."

    context = _build_monthly_context(year, month)

    prompt = f"""{context}

Faça uma análise financeira completa e honesta deste mês.

Use EXATAMENTE esta estrutura com emojis e tags HTML do Telegram:

💰 <b>Resumo Geral</b>
[Receita total, despesas totais e saldo. Negrito nos valores com <b>R$ X</b>.]

📊 <b>Maiores Gastos</b>
[As 3-4 categorias que mais pesaram, com valor em <b>negrito</b> e % do total. Uma por linha.]

🔄 <b>Fluxo de Caixa</b>
[Sobrou ou faltou? Taxa de poupança em %. Seja direto.]

⚠️ <b>Pontos de Atenção</b>
[Gasto excessivo ou fora do padrão. Se tudo ok, diga claramente.]

💡 <b>Sugestões</b>
[Máximo 3 dicas práticas baseadas nos dados reais. Prefixe cada uma com •]

IMPORTANTE: use apenas <b>, <i> e emojis. Não use markdown, asteriscos ou #.
Use os valores reais dos dados. Não invente informações."""

    return _llm(prompt, system=ANALYSIS_SYSTEM, timeout=180)


# ─── RESUMO SEMANAL ───────────────────────────────────────────────────────────

def generate_weekly_digest() -> str:
    today    = date.today()
    week_ago = (today - pd.Timedelta(days=7)).isoformat()
    df       = dm.get_expenses(start=week_ago, end=today.isoformat())

    if df.empty:
        return "Nenhum gasto nos últimos 7 dias."

    total  = df["amount"].sum()
    by_cat = df.groupby("category")["amount"].sum().sort_values(ascending=False)
    maior  = by_cat.index[0] if not by_cat.empty else "-"

    context = (
        f"Período: {week_ago} a {today}\n"
        f"Total gasto: R$ {total:.2f}\n"
        f"Transações: {len(df)}\n"
        f"Categoria que mais pesou: {maior}\n\n"
        f"Por categoria:\n{by_cat.to_string()}\n\n"
        f"Transações:\n{dm.dataframe_to_text(df, max_rows=20)}"
    )

    prompt = (
        f"{context}\n\n"
        "Faça um resumo semanal curto (máximo 10 linhas). Inclua: total gasto, "
        "principais categorias, e 1-2 observações úteis baseadas nos dados reais."
    )
    return _llm(prompt, system=ANALYSIS_SYSTEM, timeout=180)


# ─── ALERTAS ──────────────────────────────────────────────────────────────────

def check_and_format_alerts(year: int, month: int) -> str:
    alerts = dm.check_budget_alerts(year, month)
    if not alerts:
        return ""
    lines = ["Alerta de gastos:\n"]
    for a in alerts:
        status = "ESTOURADO" if a["pct"] >= 100 else f"{a['pct']}% do limite"
        lines.append(f"  {a['category'].title()}: R$ {a['spent']:.2f} / R$ {a['limit']:.2f} — {status}")
    return "\n".join(lines)


# ─── PERGUNTA LIVRE ───────────────────────────────────────────────────────────

def answer_financial_question(question: str, context_data: str) -> str:
    prompt = (
        f"Dados disponíveis:\n{context_data}\n\n"
        f"Pergunta: {question}\n\n"
        "Responda com base APENAS nos dados acima. "
        "Se os dados não forem suficientes, diga claramente o que falta."
    )
    return _llm(prompt, system=ANALYSIS_SYSTEM, timeout=180)


def answer_from_sheets(question: str) -> str:
    # Tenta pegar o mês/ano da pergunta para filtrar dados relevantes
    now   = datetime.now()
    month_keywords = {
        "janeiro":1,"fevereiro":2,"marco":3,"março":3,"abril":4,"maio":5,
        "junho":6,"julho":7,"agosto":8,"setembro":9,"outubro":10,"novembro":11,"dezembro":12
    }
    q_lower = question.lower()
    month   = next((v for k,v in month_keywords.items() if k in q_lower), now.month)
    year    = now.year

    # Busca dados com receitas incluídas para análises de fluxo
    all_data = dm.get_all_entries(
        start=f"{year}-{month:02d}-01",
        end=f"{year}-{month:02d}-31",
        limit=500
    )
    if all_data.empty:
        all_data = dm.get_all_entries(limit=500)

    if all_data.empty:
        return "Nenhum dado encontrado. Importe uma planilha com /importar <arquivo.csv>"

    # Calcula estatísticas com Pandas antes de enviar ao LLM
    expenses = all_data[all_data["type"] != "income"]
    income   = all_data[all_data["type"] == "income"]["amount"].sum()
    total_exp = expenses["amount"].sum() if not expenses.empty else 0

    summary_text = ""
    if not expenses.empty:
        by_cat = expenses.groupby("category")["amount"].sum().sort_values(ascending=False)
        summary_text = f"\nResumo por categoria:\n{by_cat.to_string()}"

    context = (
        f"Total de registros: {len(all_data)}\n"
        f"Receita total: R$ {income:.2f}\n"
        f"Despesas totais: R$ {total_exp:.2f}\n"
        f"Saldo: R$ {income - total_exp:.2f}\n"
        f"{summary_text}\n\n"
        f"Dados detalhados:\n{dm.dataframe_to_text(all_data, max_rows=100)}"
    )
    return answer_financial_question(question, context)