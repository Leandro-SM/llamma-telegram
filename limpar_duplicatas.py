"""
limpar_duplicatas.py
1. Migra o banco (adiciona coluna 'type' e 'payment' se não existirem)
2. Remove duplicatas
Execute: python limpar_duplicatas.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path("./data/expenses.db")

with sqlite3.connect(DB_PATH) as con:
    # --- Migração: adiciona colunas novas se não existirem ---
    cols = [r[1] for r in con.execute("PRAGMA table_info(expenses)").fetchall()]
    print(f"Colunas atuais: {cols}")

    if "type" not in cols:
        con.execute("ALTER TABLE expenses ADD COLUMN type TEXT DEFAULT 'expense'")
        print("Coluna 'type' adicionada.")

    if "payment" not in cols:
        con.execute("ALTER TABLE expenses ADD COLUMN payment TEXT DEFAULT ''")
        print("Coluna 'payment' adicionada.")

    # Marca como income entradas com categoria receita/salario
    con.execute("""
        UPDATE expenses SET type = 'income'
        WHERE LOWER(category) IN ('receita','salario','salário','renda','salario empresa')
        AND (type IS NULL OR type = 'expense')
    """)
    income_updated = con.execute("SELECT changes()").fetchone()[0]
    if income_updated:
        print(f"{income_updated} entradas marcadas como 'income'.")

    con.commit()

    # --- Remove duplicatas ---
    before = con.execute("SELECT COUNT(*) FROM expenses").fetchone()[0]

    con.execute("""
        DELETE FROM expenses
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM expenses
            GROUP BY date, ROUND(amount, 2), category, "desc"
        )
    """)
    con.commit()

    after = con.execute("SELECT COUNT(*) FROM expenses").fetchone()[0]

print(f"\nAntes: {before} registros")
print(f"Depois: {after} registros")
print(f"Removidos: {before - after} duplicatas")
print("\nMigração concluída. Reinicie o bot.")