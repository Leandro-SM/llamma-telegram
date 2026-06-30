import sqlite3
import pandas as pd
from pathlib import Path
from datetime import datetime, date
from typing import Optional

DB_PATH    = Path("./data/expenses.db")
SHEETS_DIR = Path("./data/sheets")


def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    SHEETS_DIR.mkdir(exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT NOT NULL,
                amount      REAL NOT NULL,
                category    TEXT NOT NULL,
                desc        TEXT,
                payment     TEXT,
                type        TEXT DEFAULT 'expense',
                source      TEXT DEFAULT 'manual',
                created     TEXT DEFAULT (datetime('now'))
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS budgets (
                category     TEXT PRIMARY KEY,
                limit_amount REAL NOT NULL
            )
        """)
        con.commit()


# ─── GASTOS ───────────────────────────────────────────────────────────────────

def add_expense(amount: float, category: str, desc: str = "",
                expense_date: Optional[str] = None, source: str = "manual",
                payment: str = "", entry_type: str = "expense") -> int:
    expense_date = expense_date or date.today().isoformat()
    amount_abs   = abs(amount)
    cat          = category.lower().strip()
    with sqlite3.connect(DB_PATH) as con:
        # Evita duplicatas exatas (mesma data, valor, categoria, desc e source)
        dup = con.execute(
            'SELECT id FROM expenses WHERE date=? AND ROUND(amount,2)=ROUND(?,2) AND category=? AND desc=? AND source=?',
            (expense_date, amount_abs, cat, desc, source)
        ).fetchone()
        if dup:
            return dup[0]  # retorna o ID existente silenciosamente
        cur = con.execute(
            "INSERT INTO expenses (date, amount, category, desc, payment, type, source) VALUES (?,?,?,?,?,?,?)",
            (expense_date, amount_abs, cat, desc, payment, entry_type, source)
        )
        con.commit()
        return cur.lastrowid


def delete_expense(expense_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        con.commit()
        return cur.rowcount > 0


def get_expenses(start: Optional[str] = None, end: Optional[str] = None,
                 category: Optional[str] = None, limit: int = 200,
                 include_income: bool = False) -> pd.DataFrame:
    query  = "SELECT * FROM expenses WHERE 1=1"
    params = []
    if not include_income:
        query += " AND type != 'income'"
    if start:
        query += " AND date >= ?"; params.append(start)
    if end:
        query += " AND date <= ?"; params.append(end)
    if category:
        query += " AND category = ?"; params.append(category.lower())
    query += f" ORDER BY date DESC LIMIT {limit}"
    with sqlite3.connect(DB_PATH) as con:
        return pd.read_sql_query(query, con, params=params)


def get_all_entries(start: Optional[str] = None, end: Optional[str] = None,
                    limit: int = 500) -> pd.DataFrame:
    """Retorna receitas E despesas — para análise completa de fluxo de caixa."""
    query  = "SELECT * FROM expenses WHERE 1=1"
    params = []
    if start:
        query += " AND date >= ?"; params.append(start)
    if end:
        query += " AND date <= ?"; params.append(end)
    query += f" ORDER BY date DESC LIMIT {limit}"
    with sqlite3.connect(DB_PATH) as con:
        return pd.read_sql_query(query, con, params=params)


def get_monthly_summary(year: int, month: int) -> pd.DataFrame:
    start = f"{year}-{month:02d}-01"
    end   = f"{year}-{month:02d}-31"
    df    = get_expenses(start=start, end=end)
    if df.empty:
        return df
    return (df.groupby("category")["amount"]
              .agg(["sum", "count"])
              .rename(columns={"sum": "total", "count": "transacoes"})
              .sort_values("total", ascending=False)
              .reset_index())


def get_monthly_income(year: int, month: int) -> float:
    start = f"{year}-{month:02d}-01"
    end   = f"{year}-{month:02d}-31"
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE type='income' AND date>=? AND date<=?",
            (start, end)
        ).fetchone()
    return row[0] if row else 0.0


def get_last_expenses(n: int = 10) -> pd.DataFrame:
    """Retorna as N despesas mais recentes (exclui receitas)."""
    with sqlite3.connect(DB_PATH) as con:
        return pd.read_sql_query(
            f"SELECT * FROM expenses WHERE type != 'income' ORDER BY date DESC, created DESC LIMIT {n}", con
        )

def get_last_income(n: int = 10) -> pd.DataFrame:
    """Retorna as N receitas mais recentes."""
    with sqlite3.connect(DB_PATH) as con:
        return pd.read_sql_query(
            f"SELECT * FROM expenses WHERE type = 'income' ORDER BY date DESC, created DESC LIMIT {n}", con
        )


# ─── LIMITES (BUDGETS) ────────────────────────────────────────────────────────

def set_budget(category: str, limit_amount: float):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR REPLACE INTO budgets (category, limit_amount) VALUES (?,?)",
                    (category.lower(), limit_amount))
        con.commit()


def get_budgets() -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as con:
        return pd.read_sql_query("SELECT * FROM budgets", con)


def check_budget_alerts(year: int, month: int) -> list[dict]:
    summary = get_monthly_summary(year, month)
    budgets = get_budgets()
    if summary.empty or budgets.empty:
        return []
    merged = summary.merge(budgets, on="category", how="inner")
    alerts = []
    for _, row in merged.iterrows():
        pct = row["total"] / row["limit_amount"]
        if pct >= 0.8:
            alerts.append({"category": row["category"], "spent": row["total"],
                           "limit": row["limit_amount"], "pct": round(pct * 100, 1)})
    return alerts


# ─── IMPORTAÇÃO DE PLANILHAS ──────────────────────────────────────────────────

# Mapeamento flexível de nomes de colunas
COL_MAP = {
    "data": "date", "valor": "amount", "categoria": "category",
    "descricao": "desc", "descrição": "desc", "description": "desc",
    "forma_pagamento": "payment", "forma pagamento": "payment",
    "pagamento": "payment", "payment_method": "payment",
    "tipo": "type"
}

def _parse_amount(raw: str) -> float:
    """
    Converte string de valor para float detectando separador decimal vs milhar.
      "1621.00"  → 1621.0   (ponto decimal)
      "1.621,00" → 1621.0   (ponto milhar, vírgula decimal)
      "-1.700"   → -1700.0  (ponto milhar sem casas decimais)
      "-230,50"  → -230.5   (vírgula decimal)
    """
    s = raw.replace("R$", "").replace(" ", "").strip()
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        parts = s.split(",")
        s = s.replace(",", "") if len(parts[-1]) == 3 and len(parts) == 2 else s.replace(",", ".")
    elif "." in s:
        parts = s.split(".")
        if len(parts[-1]) == 3 and len(parts) == 2:
            s = s.replace(".", "")
    return float(s)


def import_sheet(filepath: str | Path) -> tuple[int, list[str]]:
    """
    Importa CSV ou Excel. Suporta valores negativos como despesas,
    positivos como receitas, e a coluna Forma_Pagamento.
    Rejeita reimportação do mesmo arquivo.
    """
    filepath = Path(filepath)

    # Verifica se este arquivo já foi importado (pelo nome)
    with sqlite3.connect(DB_PATH) as con:
        already = con.execute(
            "SELECT COUNT(*) FROM expenses WHERE source = ?", (filepath.name,)
        ).fetchone()[0]
    if already > 0:
        return 0, [f"Arquivo '{filepath.name}' já foi importado ({already} registros). "
                   f"Renomeie o arquivo ou delete os registros antigos com: "
                   f"DELETE FROM expenses WHERE source='{filepath.name}'"]

    if filepath.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(filepath)
    else:
        df = pd.read_csv(filepath, sep=None, engine="python")

    # Normaliza nomes de colunas
    df.columns = [COL_MAP.get(c.lower().strip(), c.lower().strip()) for c in df.columns]

    required = {"date", "amount", "category"}
    missing  = required - set(df.columns)
    if missing:
        return 0, [f"Colunas ausentes: {missing}. Encontradas: {list(df.columns)}"]

    imported, errors = 0, []
    for i, row in df.iterrows():
        try:
            raw_amount = _parse_amount(str(row["amount"]))
            # Valor negativo = despesa, positivo = receita
            entry_type = "income" if raw_amount > 0 else "expense"
            category   = str(row["category"]).lower().strip()

            # Receita com categoria "receita" ou "salario" → força income
            if any(w in category for w in ["receita", "salario", "salário", "renda"]):
                entry_type = "income"

            add_expense(
                amount=raw_amount,
                category=category,
                desc=str(row.get("desc", "")),
                expense_date=str(row["date"])[:10],
                source=filepath.name,
                payment=str(row.get("payment", "")),
                entry_type=entry_type
            )
            imported += 1
        except Exception as e:
            errors.append(f"Linha {i+2}: {e}")

    return imported, errors


def dataframe_to_text(df: pd.DataFrame, max_rows: int = 50) -> str:
    if df.empty:
        return "Nenhum dado encontrado."
    return df.head(max_rows).to_string(index=False)
