import sqlite3
import os
import pandas as pd
from pathlib import Path
from datetime import datetime

def get_db_connection(db_name):
    """
    Crea una conexión a la base de datos de forma segura.
    """
    return sqlite3.connect(db_name)

def save_to_ledger(df, db_name):
    """
    Guarda/actualiza ledger en una base única por empresa.
    Si la base ya existe, reemplaza solo los años presentes en el upload.
    """
    work = df.copy()
    if "fecha" in work.columns:
        # Normaliza fecha de forma robusta:
        # 1) intenta ISO exacto (YYYY-MM-DD), 2) fallback dayfirst para DD/MM/YYYY.
        raw_fecha = work["fecha"]
        fechas_iso = pd.to_datetime(raw_fecha, format="%Y-%m-%d", errors="coerce")
        fechas_alt = pd.to_datetime(raw_fecha, errors="coerce", dayfirst=True)
        fechas = fechas_iso.fillna(fechas_alt)
        work["fecha"] = fechas.dt.strftime("%Y-%m-%d")
        if "comprobante" in work.columns:
            by_comp_fecha = (
                work.loc[work["fecha"].notna() & (work["fecha"].astype(str).str.strip() != "")]
                .groupby("comprobante")["fecha"]
                .first()
            )
            no_fecha = work["fecha"].isna() | (work["fecha"].astype(str).str.strip() == "")
            work.loc[no_fecha, "fecha"] = work.loc[no_fecha, "comprobante"].map(by_comp_fecha)

    incoming_years = []
    if "fecha" in work.columns:
        try:
            fechas_iso = pd.to_datetime(work["fecha"], format="%Y-%m-%d", errors="coerce")
            fechas_alt = pd.to_datetime(work["fecha"], errors="coerce", dayfirst=True)
            fechas_norm = fechas_iso.fillna(fechas_alt)
            incoming_years = sorted({str(int(y)) for y in fechas_norm.dt.year.dropna().unique() if int(y) > 1900})
        except Exception:
            incoming_years = []

    conn = get_db_connection(db_name)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger' LIMIT 1"
        ).fetchone() is not None

        if not exists:
            work.to_sql("ledger", conn, if_exists="replace", index=False)
        else:
            if incoming_years:
                # Backfill de fechas nulas con fecha del mismo comprobante
                # para que el borrado por año no deje "colas" sin fecha.
                conn.execute(
                    """
                    UPDATE ledger
                    SET fecha = (
                        SELECT l2.fecha
                        FROM ledger l2
                        WHERE l2.comprobante = ledger.comprobante
                          AND l2.fecha IS NOT NULL
                        LIMIT 1
                    )
                    WHERE fecha IS NULL
                    """
                )
                year_expr = """
                CASE
                    WHEN LENGTH(COALESCE(fecha, '')) >= 10 AND SUBSTR(COALESCE(fecha, ''), 5, 1) = '-' THEN SUBSTR(COALESCE(fecha, ''), 1, 4)
                    WHEN LENGTH(COALESCE(fecha, '')) >= 10 AND SUBSTR(COALESCE(fecha, ''), 3, 1) = '-' AND SUBSTR(COALESCE(fecha, ''), 6, 1) = '-' THEN SUBSTR(COALESCE(fecha, ''), 7, 4)
                    WHEN LENGTH(COALESCE(fecha, '')) >= 10 AND SUBSTR(COALESCE(fecha, ''), 3, 1) = '/' AND SUBSTR(COALESCE(fecha, ''), 6, 1) = '/' THEN SUBSTR(COALESCE(fecha, ''), 7, 4)
                    ELSE SUBSTR(COALESCE(fecha, ''), 1, 4)
                END
                """
                ph = ",".join(["?"] * len(incoming_years))
                conn.execute(f"DELETE FROM ledger WHERE ({year_expr}) IN ({ph})", incoming_years)

                # Limpia también filas sin fecha del mismo lote (mismo comprobante),
                # para evitar acumulación al reimportar archivos con líneas sin fecha.
                if "comprobante" in work.columns:
                    incoming_comp = [
                        str(x).strip()
                        for x in work["comprobante"].dropna().astype(str).tolist()
                        if str(x).strip()
                    ]
                    if incoming_comp:
                        uniq = sorted(set(incoming_comp))
                        chunk_size = 800
                        for i in range(0, len(uniq), chunk_size):
                            chunk = uniq[i:i + chunk_size]
                            ph_comp = ",".join(["?"] * len(chunk))
                            conn.execute(
                                f"""
                                DELETE FROM ledger
                                WHERE (fecha IS NULL OR TRIM(COALESCE(fecha, '')) = '')
                                  AND TRIM(COALESCE(comprobante, '')) IN ({ph_comp})
                                """,
                                chunk,
                            )
                work.to_sql("ledger", conn, if_exists="append", index=False)
            else:
                # Fallback defensivo si no se pudo inferir periodo.
                work.to_sql("ledger", conn, if_exists="replace", index=False)

        # Crear índices para velocidad
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cuenta ON ledger(cuenta)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nombre_cuenta ON ledger(nombre_cuenta)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_razon_social ON ledger(razon_social)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cuenta_ficha_doc ON ledger(cuenta, ficha, documento)")
        _ensure_runtime_objects(conn)
        try:
            _ensure_libros_schema(conn)
        except Exception:
            pass
        conn.commit()
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


def _ensure_runtime_objects(conn):
    """Crea ledger_ajustes y vista ledger_runtime si no existen."""
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ledger'")
    if cur.fetchone() is None:
        return False
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ledger_ajustes'")
    if cur.fetchone() is None:
        cur.execute("CREATE TABLE ledger_ajustes AS SELECT * FROM ledger WHERE 1=0")
    cur.execute("DROP VIEW IF EXISTS ledger_runtime")
    cur.execute(
        """
        CREATE VIEW ledger_runtime AS
        SELECT * FROM ledger
        UNION ALL
        SELECT * FROM ledger_ajustes
        """
    )
    conn.commit()
    return True


def _ensure_libros_schema(conn) -> None:
    """Crea tablas materializadas para libro de ventas/compras si no existen."""
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS libro_ventas_docs (\n"
                "  periodo TEXT NOT NULL,\n"
                "  fecha TEXT,\n"
                "  tipo_doc TEXT,\n"
                "  comprobante TEXT NOT NULL,\n"
                "  folio TEXT,\n"
                "  rut_ficha TEXT,\n"
                "  razon_social TEXT,\n"
                "  unidad_de_negocio TEXT,\n"
                "  exento REAL NOT NULL DEFAULT 0,\n"
                "  neto REAL NOT NULL DEFAULT 0,\n"
                "  iva REAL NOT NULL DEFAULT 0,\n"
                "  otros REAL NOT NULL DEFAULT 0,\n"
                "  total REAL NOT NULL DEFAULT 0,\n"
                "  created_at TEXT DEFAULT CURRENT_TIMESTAMP,\n"
                "  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,\n"
                "  PRIMARY KEY (periodo, comprobante)\n"
                ")")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_lv_periodo ON libro_ventas_docs(periodo)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_lv_folio ON libro_ventas_docs(folio)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_lv_rut ON libro_ventas_docs(rut_ficha)")
    # Migra columnas si la tabla ya existía antes.
    existing = {r[1] for r in cur.execute("PRAGMA table_info(libro_ventas_docs)").fetchall()}
    if "tipo_doc" not in existing:
        cur.execute("ALTER TABLE libro_ventas_docs ADD COLUMN tipo_doc TEXT")
    if "exento" not in existing:
        cur.execute("ALTER TABLE libro_ventas_docs ADD COLUMN exento REAL NOT NULL DEFAULT 0")
    if "otros" not in existing:
        cur.execute("ALTER TABLE libro_ventas_docs ADD COLUMN otros REAL NOT NULL DEFAULT 0")


def refresh_libro_ventas_from_ledger(db_name: str, clasificacion_xlsx_path: str = "clasificacion.xlsx") -> None:
    """
    Reconstruye el Libro de Ventas desde `ledger` para periodos presentes en la base.

    Regla:
    - Documento de venta = comprobante que tenga movimiento en:
      - Deudores por venta (EESS -> cuentas por cobrar -> "Deudores por venta")
      - y en Ingresos (PYG -> Detalle N1 = "Ingresos")
    - Neto = SUM(haber - debe) de cuentas de ingresos del comprobante
    - IVA  = SUM(haber - debe) de cuenta(s) IVA débito fiscal del comprobante (si existe)
    - Total= SUM(debe - haber) de deudores por venta del comprobante
    """
    import re

    conn = get_db_connection(db_name)
    try:
        _ensure_runtime_objects(conn)
        _ensure_libros_schema(conn)

        # Carga clasificación
        clasif_path = Path(clasificacion_xlsx_path)
        if not clasif_path.exists():
            return
        cdf = pd.read_excel(clasif_path)
        if cdf is None or cdf.empty:
            return
        # Normaliza cuenta desde "Cuenta Descripcion"
        if "Cuenta Descripcion" in cdf.columns and "cuenta" not in cdf.columns:
            raw = cdf["Cuenta Descripcion"].astype(str).fillna("")
            cdf["cuenta"] = raw.str.extract(r"^\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", expand=False).fillna("")
            if (cdf["cuenta"] == "").all():
                cdf["cuenta"] = raw.str.split().str[0].fillna("")
        cdf["cuenta"] = cdf.get("cuenta", "").astype(str).str.strip()
        cdf["Reporte"] = cdf.get("Reporte", "").astype(str).str.strip().str.upper()
        cdf["Detalle N1"] = cdf.get("Detalle N1", "").astype(str).str.strip().str.lower()

        ingresos_ctas = (
            cdf.loc[(cdf["Reporte"] == "PYG") & (cdf["Detalle N1"] == "ingresos"), "cuenta"]
            .dropna()
            .astype(str)
            .str.strip()
            .unique()
            .tolist()
        )
        # IVA débito fiscal: por descripción o por Detalle N1
        iva_debito_ctas = (
            cdf.loc[
                (cdf["Reporte"] == "EESS")
                & (cdf["Detalle N1"].isin(["impuestos por pagar", "impuestos"]))
                & (cdf["Cuenta Descripcion"].astype(str).str.contains("IVA Debito", case=False, na=False)),
                "cuenta",
            ]
            .dropna()
            .astype(str)
            .str.strip()
            .unique()
            .tolist()
        )
        # Deudores por venta: por descripción
        deudores_ctas = (
            cdf.loc[
                (cdf["Reporte"] == "EESS")
                & (cdf["Detalle N1"] == "cuentas por cobrar")
                & (cdf["Cuenta Descripcion"].astype(str).str.contains("Deudores por venta", case=False, na=False)),
                "cuenta",
            ]
            .dropna()
            .astype(str)
            .str.strip()
            .unique()
            .tolist()
        )

        if not ingresos_ctas or not deudores_ctas:
            return

        # Periodos presentes en ledger
        periods = pd.read_sql(
            """
            SELECT DISTINCT SUBSTR(fecha, 1, 4) AS y, SUBSTR(fecha, 6, 2) AS m
            FROM ledger
            WHERE COALESCE(TRIM(fecha), '') <> ''
            ORDER BY y, m
            """,
            conn,
        )
        if periods is None or periods.empty:
            return

        def _to_num(s):
            try:
                return float(s or 0)
            except Exception:
                return 0.0

        for _, pr in periods.iterrows():
            y = str(pr.get("y", "")).strip()
            m = str(pr.get("m", "")).strip()
            if not (y.isdigit() and m.isdigit()):
                continue
            periodo = f"{int(y):04d}{int(m):02d}"

            # Limpia periodo (solo para ledger, ajustes se recalculan en runtime por separado si luego lo requieren)
            conn.execute("DELETE FROM libro_ventas_docs WHERE periodo = ?", (periodo,))

            # Extrae candidatos: comprobantes del mes donde hay deudores y hay ingresos
            # NOTA: ledger guarda fecha ISO.
            start = f"{int(y):04d}-{int(m):02d}-01"
            # fin mes: usamos SQLite date arithmetic
            end = conn.execute("SELECT date(?, '+1 month', '-1 day')", (start,)).fetchone()[0]

            ph_ing = ",".join(["?"] * len(ingresos_ctas))
            ph_deu = ",".join(["?"] * len(deudores_ctas))
            params = [start, end] + deudores_ctas + [start, end] + ingresos_ctas
            cand = pd.read_sql(
                f"""
                WITH deu AS (
                    SELECT DISTINCT TRIM(comprobante) AS comprobante
                    FROM ledger
                    WHERE fecha >= ? AND fecha <= ?
                      AND TRIM(cuenta) IN ({ph_deu})
                ),
                ing AS (
                    SELECT DISTINCT TRIM(comprobante) AS comprobante
                    FROM ledger
                    WHERE fecha >= ? AND fecha <= ?
                      AND TRIM(cuenta) IN ({ph_ing})
                )
                SELECT deu.comprobante
                FROM deu
                INNER JOIN ing ON ing.comprobante = deu.comprobante
                """,
                conn,
                params=params,
            )
            if cand is None or cand.empty:
                continue

            comps = cand["comprobante"].dropna().astype(str).str.strip().tolist()
            comps = [c for c in comps if c]
            if not comps:
                continue

            chunk_size = 400
            for i in range(0, len(comps), chunk_size):
                chunk = comps[i : i + chunk_size]
                ph_comp = ",".join(["?"] * len(chunk))
                # Trae líneas de esos comprobantes del mes
                df_lines = pd.read_sql(
                    f"""
                    SELECT
                        fecha, TRIM(comprobante) AS comprobante, TRIM(cuenta) AS cuenta,
                        COALESCE(NULLIF(TRIM(ficha), ''), '') AS ficha,
                        COALESCE(NULLIF(TRIM(razon_social), ''), '') AS razon_social,
                        COALESCE(NULLIF(TRIM(unidad_de_negocio), ''), '') AS unidad_de_negocio,
                        COALESCE(NULLIF(TRIM(proyecto), ''), '') AS proyecto,
                        COALESCE(NULLIF(TRIM(documento), ''), '') AS documento,
                        COALESCE(NULLIF(TRIM(doc_referencia), ''), '') AS doc_referencia,
                        COALESCE(NULLIF(TRIM(concepto), ''), '') AS concepto,
                        COALESCE(CAST(debe AS REAL), 0) AS debe,
                        COALESCE(CAST(haber AS REAL), 0) AS haber
                    FROM ledger
                    WHERE fecha >= ? AND fecha <= ?
                      AND TRIM(comprobante) IN ({ph_comp})
                    """,
                    conn,
                    params=[start, end] + chunk,
                )
                if df_lines is None or df_lines.empty:
                    continue

                # Agrega por comprobante
                for comp, g in df_lines.groupby("comprobante"):
                    # totales por tipo de cuenta
                    g_ing = g[g["cuenta"].isin(ingresos_ctas)]
                    g_deu = g[g["cuenta"].isin(deudores_ctas)]
                    g_iva = g[g["cuenta"].isin(iva_debito_ctas)] if iva_debito_ctas else g.iloc[0:0]

                    neto = float((g_ing["haber"] - g_ing["debe"]).sum()) if not g_ing.empty else 0.0
                    total = float((g_deu["debe"] - g_deu["haber"]).sum()) if not g_deu.empty else 0.0
                    iva = float((g_iva["haber"] - g_iva["debe"]).sum()) if not g_iva.empty else 0.0
                    exento = 0.0
                    otros = 0.0

                    # Rut y razón social: prioriza línea de deudores
                    ficha = ""
                    rs = ""
                    und = ""
                    fecha = ""
                    folio = ""
                    tipo_doc = ""
                    if not g_deu.empty:
                        r0 = g_deu.iloc[0]
                        ficha = str(r0.get("ficha", "") or "").strip()
                        rs = str(r0.get("razon_social", "") or "").strip()
                        und = str(r0.get("unidad_de_negocio", "") or r0.get("proyecto", "") or "").strip()
                        fecha = str(r0.get("fecha", "") or "").strip()
                        folio = str(r0.get("documento", "") or r0.get("doc_referencia", "") or "").strip()
                    if not ficha:
                        r0 = g.iloc[0]
                        ficha = str(r0.get("ficha", "") or "").strip()
                        rs = str(r0.get("razon_social", "") or "").strip()
                        und = str(r0.get("unidad_de_negocio", "") or r0.get("proyecto", "") or "").strip()
                        fecha = str(r0.get("fecha", "") or "").strip()
                        folio = str(r0.get("documento", "") or r0.get("doc_referencia", "") or "").strip()
                    if not folio:
                        # Fallback: intenta extraer "folio 123" desde concepto
                        concept = " ".join([str(x or "") for x in g["concepto"].head(5).tolist()])
                        m_f = re.search(r"folio\s+([0-9]+)", concept, flags=re.IGNORECASE)
                        if m_f:
                            folio = m_f.group(1)
                    # Tipo de documento: desde folio o concepto (ej: "Factura Electrónica")
                    probe = " ".join([str(x or "") for x in g["concepto"].head(5).tolist()] + [str(folio or "")])
                    probe_u = probe.upper()
                    if "FACTURA" in probe_u:
                        tipo_doc = "FACTURA ELECTRÓNICA" if "ELECTR" in probe_u else "FACTURA"
                    elif "BOLETA" in probe_u:
                        tipo_doc = "BOLETA ELECTRÓNICA" if "ELECTR" in probe_u else "BOLETA"
                    else:
                        tipo_doc = "OTRO"

                    conn.execute(
                        """
                        INSERT OR REPLACE INTO libro_ventas_docs
                            (periodo, fecha, tipo_doc, comprobante, folio, rut_ficha, razon_social, unidad_de_negocio, exento, neto, iva, otros, total, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        (
                            periodo,
                            fecha,
                            tipo_doc,
                            comp,
                            folio,
                            ficha,
                            rs,
                            und,
                            exento,
                            neto,
                            iva,
                            otros,
                            total,
                        ),
                    )
        conn.commit()
    finally:
        conn.close()


def ensure_runtime_objects(db_name):
    conn = get_db_connection(db_name)
    try:
        return _ensure_runtime_objects(conn)
    finally:
        conn.close()


def _ensure_account_reviews_schema(conn):
    """Garantiza esquema para notas de revisión de cuentas."""
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='account_reviews'")
    exists = cur.fetchone() is not None

    if not exists:
        conn.execute(
            """
            CREATE TABLE account_reviews (
                cuenta TEXT NOT NULL,
                fecha_balance TEXT NOT NULL,
                aprobado INTEGER NOT NULL DEFAULT 0,
                nota TEXT NOT NULL,
                usuario TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (cuenta, fecha_balance)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_account_reviews_fecha ON account_reviews(fecha_balance)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_account_reviews_updated ON account_reviews(updated_at)")
        return

    info = cur.execute("PRAGMA table_info(account_reviews)").fetchall()
    cols = {str(r[1]).lower() for r in info}
    pk_cols = [str(r[1]).lower() for r in info if int(r[5] or 0) > 0]
    needs_migration = ("fecha_balance" not in cols) or (pk_cols == ["cuenta"])

    if needs_migration:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS account_reviews_v2 (
                cuenta TEXT NOT NULL,
                fecha_balance TEXT NOT NULL,
                aprobado INTEGER NOT NULL DEFAULT 0,
                nota TEXT NOT NULL,
                usuario TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (cuenta, fecha_balance)
            )
            """
        )
        cuenta_expr = "cuenta" if "cuenta" in cols else "''"
        fecha_expr = "COALESCE(NULLIF(TRIM(fecha_balance), ''), 'GLOBAL')" if "fecha_balance" in cols else "'GLOBAL'"
        aprobado_expr = "COALESCE(aprobado, 0)" if "aprobado" in cols else "0"
        nota_expr = "COALESCE(nota, '')" if "nota" in cols else "''"
        usuario_expr = "usuario" if "usuario" in cols else "''"
        updated_expr = "updated_at" if "updated_at" in cols else "CURRENT_TIMESTAMP"
        conn.execute(
            f"""
            INSERT OR REPLACE INTO account_reviews_v2 (cuenta, fecha_balance, aprobado, nota, usuario, updated_at)
            SELECT
                {cuenta_expr} AS cuenta,
                {fecha_expr} AS fecha_balance,
                {aprobado_expr} AS aprobado,
                {nota_expr} AS nota,
                {usuario_expr} AS usuario,
                {updated_expr} AS updated_at
            FROM account_reviews
            """
        )
        conn.execute("DROP TABLE account_reviews")
        conn.execute("ALTER TABLE account_reviews_v2 RENAME TO account_reviews")

    cols = {str(r[1]).lower() for r in cur.execute("PRAGMA table_info(account_reviews)").fetchall()}
    if "usuario" not in cols:
        conn.execute("ALTER TABLE account_reviews ADD COLUMN usuario TEXT")
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE account_reviews ADD COLUMN updated_at TEXT")
    conn.execute("UPDATE account_reviews SET fecha_balance = 'GLOBAL' WHERE COALESCE(TRIM(fecha_balance), '') = ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_account_reviews_fecha ON account_reviews(fecha_balance)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_account_reviews_updated ON account_reviews(updated_at)")


def ensure_account_reviews_schema(db_name):
    conn = get_db_connection(db_name)
    try:
        _ensure_account_reviews_schema(conn)
        conn.commit()
        return True
    finally:
        conn.close()


def _normalize_fecha_balance(fecha_balance):
    raw = str(fecha_balance or "").strip()
    if not raw:
        return "GLOBAL"
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        return "GLOBAL"


def save_account_review(db_name, cuenta, aprobado, nota, usuario, fecha_balance=None):
    conn = get_db_connection(db_name)
    try:
        _ensure_account_reviews_schema(conn)
        fbal = _normalize_fecha_balance(fecha_balance)
        conn.execute(
            """
            INSERT INTO account_reviews (cuenta, fecha_balance, aprobado, nota, usuario, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(cuenta, fecha_balance) DO UPDATE SET
                aprobado = excluded.aprobado,
                nota = excluded.nota,
                usuario = excluded.usuario,
                updated_at = CURRENT_TIMESTAMP
            """,
            (str(cuenta), fbal, 1 if aprobado else 0, str(nota or "").strip(), str(usuario or "")),
        )
        conn.commit()
    finally:
        conn.close()


def get_account_review(db_name, cuenta, fecha_balance=None):
    conn = get_db_connection(db_name)
    try:
        _ensure_account_reviews_schema(conn)
        fbal = _normalize_fecha_balance(fecha_balance)
        cur = conn.cursor()
        row = None
        if fbal != "GLOBAL":
            cur.execute(
                """
                SELECT cuenta, fecha_balance, aprobado, nota, usuario, updated_at
                FROM account_reviews
                WHERE cuenta = ? AND fecha_balance = ?
                """,
                (str(cuenta), fbal),
            )
            row = cur.fetchone()
        if not row:
            cur.execute(
                """
                SELECT cuenta, fecha_balance, aprobado, nota, usuario, updated_at
                FROM account_reviews
                WHERE cuenta = ? AND fecha_balance = 'GLOBAL'
                """,
                (str(cuenta),),
            )
            row = cur.fetchone()
        if not row:
            cur.execute(
                """
                SELECT cuenta, fecha_balance, aprobado, nota, usuario, updated_at
                FROM account_reviews
                WHERE cuenta = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (str(cuenta),),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "cuenta": row[0],
            "fecha_balance": row[1] or "",
            "aprobado": int(row[2] or 0),
            "nota": row[3] or "",
            "usuario": row[4] or "",
            "updated_at": row[5] or "",
        }
    finally:
        conn.close()


def list_account_reviews(db_name, only_no_aprobadas=False, fecha_balance=None):
    conn = get_db_connection(db_name)
    try:
        _ensure_account_reviews_schema(conn)
        fbal = _normalize_fecha_balance(fecha_balance) if fecha_balance else None
        q = """
        SELECT cuenta, fecha_balance, aprobado, nota, usuario, updated_at
        FROM account_reviews
        """
        params = []
        where = []
        if fbal:
            where.append("fecha_balance = ?")
            params.append(fbal)
        if only_no_aprobadas:
            where.append("aprobado = 0")
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY updated_at DESC"
        return pd.read_sql(q, conn, params=params)
    finally:
        conn.close()

# --- GESTIÓN DE USUARIOS (NUEVO) ---

def setup_users_db():
    """Crea la base de datos de usuarios y el primer administrador si no existe."""
    db_path = "users.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)
    # Crear usuario por defecto 'will' / '7784' si la tabla está vacía
    cursor.execute("SELECT COUNT(*) FROM users")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO users (username, password) VALUES (?, ?)", ("will", "7784"))
    conn.commit()
    conn.close()

def verify_login(username, password):
    """Verifica si el usuario y contraseña son correctos."""
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ? AND password = ?", (username, password))
    user = cursor.fetchone()
    conn.close()
    return user is not None

def create_user(username, password):
    """Crea un nuevo usuario."""
    try:
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False

def update_password(username, new_password):
    """Cambia la contraseña de un usuario."""
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET password = ? WHERE username = ?", (new_password, username))
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated

def delete_user(username):
    """Elimina un usuario."""
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE username = ?", (username,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted

def get_all_users():
    """Devuelve la lista de nombres de usuario."""
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users")
    users = [row[0] for row in cursor.fetchall()]
    conn.close()
    return users
