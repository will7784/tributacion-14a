import sqlite3
import os
import gc
import pandas as pd
from pathlib import Path

def get_db_connection(db_name):
    """
    Crea una conexión a la base de datos de forma segura.
    """
    return sqlite3.connect(db_name)

def save_to_ledger(df, db_name):
    """
    Guarda un DataFrame en la tabla 'ledger' de la base de datos SQLite.
    Versión Windows-safe para re-crear la base si ya existe.
    """
    # 1. Intentar borrar si existe (Windows-safe)
    if os.path.exists(db_name):
        # Forzar limpieza de GC para liberar posibles locks de sqlite
        gc.collect()
        try:
            os.remove(db_name)
        except PermissionError:
            # Si falla, simplemente intentamos usar replace en to_sql
            pass

    conn = get_db_connection(db_name)
    try:
        df.to_sql("ledger", conn, if_exists="replace", index=False)
        # Crear índices para velocidad
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cuenta ON ledger(cuenta)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nombre_cuenta ON ledger(nombre_cuenta)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_razon_social ON ledger(razon_social)")
    finally:
        conn.close()

def run_query(query, db_name, params=None):
    """
    Ejecuta una consulta SQL y devuelve un DataFrame.
    """
    conn = get_db_connection(db_name)
    try:
        df = pd.read_sql(query, conn, params=params)
        return df
    finally:
        conn.close()
