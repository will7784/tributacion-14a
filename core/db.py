import sqlite3
import os
import pandas as pd
from pathlib import Path

DB_DIR = Path(os.environ.get("DATA_DIR", "."))


def get_db_path(rut: str) -> Path:
    clean = rut.replace(".", "").replace("-", "").upper()
    return DB_DIR / f"{clean}_14a.db"


def get_connection(rut: str):
    db_path = get_db_path(rut)
    return sqlite3.connect(db_path)


def init_db(rut: str):
    conn = get_connection(rut)
    cursor = conn.cursor()

    # Empresa
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS empresa (
            rut TEXT PRIMARY KEY,
            nombre TEXT,
            actividad_principal TEXT,
            entidad_supervisora TEXT,
            anio_ifrs INTEGER,
            folio_balance_ini TEXT,
            folio_balance_fin TEXT,
            rep_legal_nombre TEXT,
            rep_legal_rut TEXT
        )
        """
    )

    # Homologación: cuenta del cliente → cuenta base
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS plan_cuentas (
            cuenta_local TEXT PRIMARY KEY,
            nombre_local TEXT,
            cuenta_base TEXT
        )
        """
    )

    # Clasificación (placeholder para futuro)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS clasificacion (
            cuenta_base TEXT PRIMARY KEY,
            tipo_reporte TEXT,
            categoria TEXT
        )
        """
    )

    # Ledger (movimientos importados y comprobantes CYT)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT,
            comprobante TEXT,
            tipo_comprobante TEXT,
            cuenta TEXT,
            nombre_cuenta TEXT,
            debe REAL DEFAULT 0,
            haber REAL DEFAULT 0,
            concepto TEXT,
            rut_ficha TEXT,
            razon_social TEXT,
            documento TEXT,
            fecha_venc TEXT,
            unidad_negocio TEXT,
            tipo_movimiento TEXT,
            numero_movimiento TEXT,
            origen TEXT DEFAULT 'IMPORTADO'
        )
        """
    )

    # Comprobantes (cabecera)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS comprobantes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT,
            glosa TEXT,
            tipo_norma TEXT DEFAULT 'CYT',
            numero TEXT
        )
        """
    )

    # Comprobante líneas
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS comprobante_lineas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comprobante_id INTEGER,
            cuenta TEXT,
            debe REAL DEFAULT 0,
            haber REAL DEFAULT 0,
            concepto TEXT,
            tipo_norma TEXT DEFAULT 'CYT',
            FOREIGN KEY (comprobante_id) REFERENCES comprobantes(id)
        )
        """
    )

    # Ajustes tributarios DJ1926 Sección B
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ajustes_tributarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo_sii TEXT,
            descripcion TEXT,
            monto REAL DEFAULT 0,
            tipo_ajuste INTEGER,
            cuenta_afectada TEXT
        )
        """
    )

    # Ajustes patrimonio DJ1926 Sección C
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ajustes_patrimonio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            id_cod_partida TEXT,
            cuenta_local TEXT,
            nombre_cuenta TEXT,
            ifrs_sd REAL DEFAULT 0,
            ifrs_sc REAL DEFAULT 0,
            ejercicio_sd REAL DEFAULT 0,
            ejercicio_sc REAL DEFAULT 0
        )
        """
    )

    # DJ1847 overrides: cod_f22 y valor_tributario_manual por cuenta
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS dj1847_overrides (
            cuenta TEXT PRIMARY KEY,
            cod_f22 TEXT,
            valor_tributario_manual REAL
        )
        """
    )

    # DJ1847 periodo: datos de Sección B por empresa/periodo
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS dj1847_periodo (
            rut TEXT,
            periodo TEXT,
            actividad_economica TEXT,
            entidad_supervisora TEXT,
            anio_ifrs INTEGER,
            folio_ini INTEGER,
            folio_fin INTEGER,
            ajustes_rli INTEGER DEFAULT 2,
            PRIMARY KEY (rut, periodo)
        )
        """
    )

    # Folios usados: tracking de folios por empresa para evitar reutilizar
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS folios_usados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rut TEXT,
            tipo_dj TEXT,
            periodo TEXT,
            folio_ini INTEGER,
            folio_fin INTEGER,
            fecha_uso TEXT
        )
        """
    )

    conn.commit()
    conn.close()


def guardar_empresa(rut: str, nombre: str, **kwargs):
    init_db(rut)
    conn = get_connection(rut)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO empresa (rut, nombre, actividad_principal, entidad_supervisora, anio_ifrs, folio_balance_ini, folio_balance_fin, rep_legal_nombre, rep_legal_rut)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rut,
            nombre,
            kwargs.get("actividad_principal"),
            kwargs.get("entidad_supervisora"),
            kwargs.get("anio_ifrs"),
            kwargs.get("folio_balance_ini"),
            kwargs.get("folio_balance_fin"),
            kwargs.get("rep_legal_nombre"),
            kwargs.get("rep_legal_rut"),
        ),
    )
    conn.commit()
    conn.close()


def get_empresa(rut: str):
    conn = get_connection(rut)
    df = pd.read_sql("SELECT * FROM empresa WHERE rut = ?", conn, params=(rut,))
    conn.close()
    return df.iloc[0].to_dict() if not df.empty else None


def guardar_ledger(df: pd.DataFrame, rut: str):
    init_db(rut)
    conn = get_connection(rut)
    df.to_sql("ledger", conn, if_exists="append", index=False)
    conn.close()


def limpiar_ledger(rut: str):
    init_db(rut)
    conn = get_connection(rut)
    conn.execute("DELETE FROM ledger WHERE origen = 'IMPORTADO'")
    conn.commit()
    conn.close()


def get_ledger(rut: str) -> pd.DataFrame:
    conn = get_connection(rut)
    df = pd.read_sql("SELECT * FROM ledger", conn)
    conn.close()
    return df


def get_comprobantes(rut: str) -> pd.DataFrame:
    conn = get_connection(rut)
    df = pd.read_sql("SELECT * FROM comprobantes", conn)
    conn.close()
    return df


def get_comprobante_lineas(rut: str, comprobante_id: int = None) -> pd.DataFrame:
    conn = get_connection(rut)
    if comprobante_id:
        df = pd.read_sql(
            "SELECT * FROM comprobante_lineas WHERE comprobante_id = ?",
            conn,
            params=(comprobante_id,),
        )
    else:
        df = pd.read_sql("SELECT * FROM comprobante_lineas", conn)
    conn.close()
    return df


def guardar_comprobante(rut: str, fecha: str, glosa: str, tipo_norma: str, numero: str, lineas: list):
    init_db(rut)
    conn = get_connection(rut)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO comprobantes (fecha, glosa, tipo_norma, numero) VALUES (?, ?, ?, ?)",
        (fecha, glosa, tipo_norma, numero),
    )
    comp_id = cursor.lastrowid
    for linea in lineas:
        cursor.execute(
            """
            INSERT INTO comprobante_lineas (comprobante_id, cuenta, debe, haber, concepto, tipo_norma)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (comp_id, linea["cuenta"], linea.get("debe", 0), linea.get("haber", 0), linea.get("concepto", ""), linea.get("tipo_norma", tipo_norma)),
        )
    conn.commit()
    conn.close()
    return comp_id


def get_ajustes_tributarios(rut: str) -> pd.DataFrame:
    conn = get_connection(rut)
    df = pd.read_sql("SELECT * FROM ajustes_tributarios", conn)
    conn.close()
    return df


def guardar_ajustes_tributarios(rut: str, df: pd.DataFrame):
    init_db(rut)
    conn = get_connection(rut)
    df.to_sql("ajustes_tributarios", conn, if_exists="replace", index=False)
    conn.close()


def get_plan_cuentas(rut: str) -> pd.DataFrame:
    conn = get_connection(rut)
    df = pd.read_sql("SELECT * FROM plan_cuentas", conn)
    conn.close()
    return df


def guardar_plan_cuentas(rut: str, df: pd.DataFrame):
    init_db(rut)
    conn = get_connection(rut)
    df.to_sql("plan_cuentas", conn, if_exists="replace", index=False)
    conn.close()


def get_clasificacion(rut: str) -> pd.DataFrame:
    conn = get_connection(rut)
    df = pd.read_sql("SELECT * FROM clasificacion", conn)
    conn.close()
    return df


def guardar_clasificacion(rut: str, df: pd.DataFrame):
    init_db(rut)
    conn = get_connection(rut)
    df.to_sql("clasificacion", conn, if_exists="replace", index=False)
    conn.close()


def get_dj1847_overrides(rut: str) -> pd.DataFrame:
    conn = get_connection(rut)
    df = pd.read_sql("SELECT * FROM dj1847_overrides", conn)
    conn.close()
    return df


def guardar_dj1847_overrides(rut: str, df: pd.DataFrame):
    init_db(rut)
    conn = get_connection(rut)
    df.to_sql("dj1847_overrides", conn, if_exists="replace", index=False)
    conn.close()


def get_dj1847_periodo(rut: str, periodo: str) -> dict:
    conn = get_connection(rut)
    df = pd.read_sql("SELECT * FROM dj1847_periodo WHERE rut = ? AND periodo = ?", conn, params=(rut, periodo))
    conn.close()
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def guardar_dj1847_periodo(rut: str, periodo: str, data: dict):
    init_db(rut)
    conn = get_connection(rut)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO dj1847_periodo
        (rut, periodo, actividad_economica, entidad_supervisora, anio_ifrs, folio_ini, folio_fin, ajustes_rli)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rut,
            periodo,
            data.get("actividad_economica", ""),
            data.get("entidad_supervisora", "NO APLICA"),
            data.get("anio_ifrs", 0),
            data.get("folio_ini"),
            data.get("folio_fin"),
            data.get("ajustes_rli", 2),
        ),
    )
    conn.commit()
    conn.close()


def get_folios_usados(rut: str) -> pd.DataFrame:
    conn = get_connection(rut)
    df = pd.read_sql("SELECT * FROM folios_usados WHERE rut = ? ORDER BY fecha_uso DESC", conn, params=(rut,))
    conn.close()
    return df


def guardar_folio_usado(rut: str, tipo_dj: str, periodo: str, folio_ini: int, folio_fin: int):
    init_db(rut)
    conn = get_connection(rut)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO folios_usados (rut, tipo_dj, periodo, folio_ini, folio_fin, fecha_uso)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (rut, tipo_dj, periodo, folio_ini, folio_fin, datetime.now().strftime("%Y-%m-%d")),
    )
    conn.commit()
    conn.close()
