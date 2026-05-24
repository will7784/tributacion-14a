import os
import subprocess
import pandas as pd
import re
from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Footer, Static, Button, DataTable, Label, Input, Select, Checkbox
from textual.containers import Container, Horizontal, Vertical, Grid
from textual.screen import Screen, ModalScreen
from textual.binding import Binding

from app.core.db import (
    run_query,
    save_to_ledger,
    setup_users_db,
    verify_login,
    create_user,
    delete_user,
    get_all_users,
    ensure_runtime_objects,
    ensure_account_reviews_schema,
    list_account_reviews,
    get_account_review,
    save_account_review,
    refresh_libro_ventas_from_ledger,
)
from app.reports.excel import (
    export_balance_8_columnas,
    export_report_to_excel,
    export_libro_mayor,
    export_dataframe_simple_excel,
    export_pendientes_cuenta,
    export_informe_gestion_excel,
)

# Estado de sesión compartido (importado de main o replicado)
session = {
    "user": None,
    "erp": "KAME",
    "db_name": None,
    "empresa": "No seleccionada",
    "rut": ""
}

_CLASIF_CACHE = None


def _rut_to_kame_db_name(rut: str) -> str:
    clean = re.sub(r"[^0-9Kk]", "", str(rut or "")).upper()
    return f"{clean}_kame.db" if clean else "empresa_kame.db"

def format_miles_contable(n):
    """Miles con punto, negativos entre paréntesis, cero o vacío como guión (criterio referencia PYG/EESS)."""
    try:
        if n is None:
            return "-"
        if isinstance(n, (float, int)) and pd.isna(n):
            return "-"
        v = float(n)
        if abs(v) < 0.5:
            return "-"
        abs_s = f"{abs(v):,.0f}".replace(",", ".")
        if v < 0:
            return f"({abs_s})"
        return abs_s
    except (ValueError, TypeError):
        return "-"


def format_miles(n):
    """Alias de formato contable para totales y columnas monetarias."""
    return format_miles_contable(n)


def _clean_text_cell(val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    s = str(val).strip()
    if s.lower() in ("nan", "none", "<na>"):
        return ""
    return s


def _sql_ledger_fecha_eff(alias: str = "l") -> str:
    """
    Fecha efectiva por línea: la de la fila o, si viene vacía, la de otra línea del mismo comprobante.
    Misma lógica que balance/mayor; evita excluir movimientos y desbalancear EESS/PYG al filtrar por mes.
    """
    return f"""COALESCE(
      NULLIF(TRIM({alias}.fecha), ''),
      (SELECT TRIM(l2.fecha) FROM ledger_runtime l2
       WHERE l2.comprobante = {alias}.comprobante
         AND COALESCE(TRIM(l2.fecha), '') <> ''
       LIMIT 1)
    )"""


def _prepare_informe_detail_df(work: pd.DataFrame) -> pd.DataFrame:
    """Evita 'nan' en pantalla/export y completa nombres de cuenta."""
    w = work.copy()
    for col in ("Detalle N1", "Detalle N2", "nombre_cuenta"):
        if col in w.columns:
            w[col] = w[col].map(_clean_text_cell)
    if "nombre_cuenta" in w.columns and "cuenta" in w.columns:
        mask = w["nombre_cuenta"].str.len() == 0
        w.loc[mask, "nombre_cuenta"] = w.loc[mask, "cuenta"].astype(str)
    return w


def _ordered_n1n2_from_clasif(cf: pd.DataFrame):
    """Orden de filas según la primera aparición en `clasificacion.xlsx` (sin duplicar rubros)."""
    keys = []
    seen = set()
    for _, r in cf.iterrows():
        n1 = _clean_text_cell(r.get("Detalle N1", ""))
        n2 = _clean_text_cell(r.get("Detalle N2", ""))
        k = (n1, n2)
        if k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


def _group_sum_lookup(
    work: pd.DataFrame, value_col: str, keys: list[tuple[str, str]]
) -> pd.DataFrame:
    """Suma `value_col` por (Detalle N1, Detalle N2) y devuelve filas en el orden de `clasificacion`."""
    w = work.copy()
    if "Detalle N1" not in w.columns or "Detalle N2" not in w.columns:
        return pd.DataFrame(columns=["Detalle N1", "Detalle N2", "Acumulado"])
    w["Detalle N1"] = w["Detalle N1"].map(_clean_text_cell)
    w["Detalle N2"] = w["Detalle N2"].map(_clean_text_cell)
    w[value_col] = pd.to_numeric(w[value_col], errors="coerce").fillna(0.0)
    grouped = w.groupby(["Detalle N1", "Detalle N2"], dropna=False)[value_col].sum()
    rows: list = []
    for n1, n2 in keys:
        try:
            val = float(grouped.loc[(n1, n2)])
        except (KeyError, TypeError, IndexError):
            val = 0.0
        rows.append({"Detalle N1": n1, "Detalle N2": n2, "Acumulado": val})
    return pd.DataFrame(rows)


def _build_pyg_resumen_df(work: pd.DataFrame, cf_pyg: pd.DataFrame) -> pd.DataFrame:
    """Resumen PYG: mismos rubros y orden que en `clasificacion` (PYG), con montos del mes."""
    keys = _ordered_n1n2_from_clasif(cf_pyg)
    if not keys:
        return pd.DataFrame(columns=["Detalle N1", "Detalle N2", "Acumulado"])
    return _group_sum_lookup(work, "monto", keys)


def _eess_n1_es_patrimonio_en_pasivos(n1: str) -> bool:
    """Cuentas 2.x que en la práctica son patrimonio (van después de TOTAL PASIVOS)."""
    L = _clean_text_cell(n1).lower()
    if not L:
        return False
    if "patrimonio inicial" in L:
        return True
    if "dividendo" in L:
        return True
    return False


def _ordered_n1_eess(
    cf_eess: pd.DataFrame,
    w: pd.DataFrame,
    bucket: str,
) -> list:
    """
    bucket: 'A' activos 1.x, 'P' pasivos 2.x (sin patrimonio-inicial/dividendos), 'E' patrimonio 2.x+3.x
    Orden = primera aparición en clasificación; luego faltantes alfabético.
    """
    grouped_all = w.groupby(["_bucket", "Detalle N1"], dropna=False)["saldo"].sum()
    ordered = []
    seen = set()
    for _, r in cf_eess.iterrows():
        cuenta = str(r.get("cuenta", "")).strip()
        if not cuenta:
            continue
        sec = cuenta[0]
        n1 = _clean_text_cell(r.get("Detalle N1", ""))
        b = _eess_row_bucket(sec, n1)
        if b != bucket:
            continue
        if n1 not in seen:
            seen.add(n1)
            ordered.append(n1)
    extra = []
    for idx in grouped_all.index:
        bb, n1 = idx[0], idx[1]
        if bb != bucket:
            continue
        if n1 not in seen:
            extra.append(n1)
            seen.add(n1)
    extra.sort(key=lambda x: str(x).lower())
    return ordered + extra


def _eess_row_bucket(sec: str, n1: str) -> str:
    if sec == "1":
        return "A"
    if sec == "2":
        return "E" if _eess_n1_es_patrimonio_en_pasivos(n1) else "P"
    if sec == "3":
        return "E"
    return "R"


def _build_eess_resumen_df(
    work: pd.DataFrame, cf_eess: pd.DataFrame, utilidad_pyg: float
) -> pd.DataFrame:
    """
    EESS: Activos (1.x). Pasivos y patrimonio con el mismo criterio de presentación (*-1 al saldo neto).
    La línea 'Utilidad o (Pérdida) del periodo' es el resultado PYG YTD (misma base que el informe PYG).
    TOTAL PATRIMONIO = suma patrimonio (presentación) + utilidad PYG. Se informa diferencia de cuadre
    si Activo ≠ Pasivo + Patrimonio (por datos o cuentas fuera de modelo).
    """
    col_cls = " Clasificacion"
    col_sp = " "

    w = work.copy()
    w["Detalle N1"] = w["Detalle N1"].map(_clean_text_cell)
    w["saldo"] = pd.to_numeric(w["saldo"], errors="coerce").fillna(0.0)
    w["_sec"] = w["cuenta"].astype(str).str.strip().str[0]
    w["_bucket"] = w.apply(lambda r: _eess_row_bucket(str(r["_sec"]), str(r["Detalle N1"])), axis=1)

    ga = w[w["_bucket"] == "A"].groupby("Detalle N1", dropna=False)["saldo"].sum()
    gp = w[w["_bucket"] == "P"].groupby("Detalle N1", dropna=False)["saldo"].sum()
    ge = w[w["_bucket"] == "E"].groupby("Detalle N1", dropna=False)["saldo"].sum()

    TA = float(ga.sum())
    TP_disp_lines = {n: float(-gp.loc[n]) for n in gp.index}
    TP_disp = float(sum(TP_disp_lines.values()))

    PE_disp_lines = {n: float(-ge.loc[n]) for n in ge.index}
    PE_flipped_sum = float(sum(PE_disp_lines.values()))
    U = float(utilidad_pyg)
    TPat = PE_flipped_sum + U
    pas_mas_pat = TP_disp + TPat
    cuadre_diff = float(TA - pas_mas_pat)

    rows = []

    def add_row(label: str, acum: float, style: str = ""):
        rows.append({col_cls: label, col_sp: "", "Acumulado": float(acum), "_style": style})

    for n1 in _ordered_n1_eess(cf_eess, w, "A"):
        v = float(ga.loc[n1]) if n1 in ga.index else 0.0
        add_row(n1, v, "")

    add_row("TOTAL ACTIVOS", TA, "blue")

    for n1 in _ordered_n1_eess(cf_eess, w, "P"):
        v = TP_disp_lines.get(n1, 0.0)
        add_row(n1, v, "")

    add_row("TOTAL PASIVOS", TP_disp, "dark")

    def _equity_n1_ordered(cf: pd.DataFrame, ge_s: pd.Series) -> list:
        """Patrimonio inicial, dividendos, resto en orden de clasificación."""
        cf_ord = []
        seen = set()
        for _, r in cf.iterrows():
            cuenta = str(r.get("cuenta", "")).strip()
            if not cuenta or cuenta[0] not in ("2", "3"):
                continue
            n1 = _clean_text_cell(r.get("Detalle N1", ""))
            if n1 in seen:
                continue
            if n1 not in ge_s.index:
                continue
            seen.add(n1)
            cf_ord.append(n1)
        for n1 in sorted(ge_s.index, key=lambda x: str(x).lower()):
            if n1 not in seen:
                cf_ord.append(n1)
        inicial = [x for x in cf_ord if "patrimonio inicial" in x.lower()]
        div = [x for x in cf_ord if "dividendo" in x.lower() and x not in inicial]
        otros = [x for x in cf_ord if x not in inicial and x not in div]
        return inicial + div + otros

    eq_order = _equity_n1_ordered(cf_eess, ge)
    for n1 in eq_order:
        if n1 not in ge.index:
            continue
        add_row(n1, PE_disp_lines.get(n1, 0.0), "")

    add_row("Utilidad o (Pérdida) del periodo", U, "")
    add_row("TOTAL PATRIMONIO", TPat, "dark")

    add_row("PASIVO + PATRIMONIO", pas_mas_pat, "blue")

    rest = w[w["_bucket"] == "R"]
    if not rest.empty:
        rg = rest.groupby("Detalle N1", dropna=False)["saldo"].sum()
        for n1 in sorted(rg.index, key=lambda x: str(x).lower()):
            add_row(str(n1) if pd.notna(n1) else "", float(rg.loc[n1]), "")
        add_row("TOTAL (cuentas no 1/2/3)", float(rg.sum()), "dark")

    df = pd.DataFrame(rows)
    df.attrs["cuadre_diff"] = cuadre_diff
    df.attrs["ta"] = TA
    df.attrs["tp_disp"] = TP_disp
    df.attrs["tpat"] = TPat
    df.attrs["utilidad_periodo"] = U
    df.attrs["pyg_ytd"] = U
    return df


def _compute_pyg_net_ytd(db_name: str, clasif: pd.DataFrame, end_iso: str) -> float:
    """Resultado PYG acumulado año calendario hasta end_iso (referencia vs utilidad en cuadre)."""
    from datetime import date

    try:
        end = date.fromisoformat(end_iso[:10])
    except Exception:
        return 0.0
    start_iso = date(end.year, 1, 1).isoformat()
    cf = clasif[clasif["Reporte"] == "PYG"].copy()
    if cf.empty:
        return 0.0
    fe = _sql_ledger_fecha_eff("l")
    q = f"""
    WITH x AS (
        SELECT l.*, {fe} AS fecha_eff
        FROM ledger_runtime l
    )
    SELECT
        TRIM(cuenta) AS cuenta,
        SUM(COALESCE(CAST(debe AS REAL), 0) - COALESCE(CAST(haber AS REAL), 0)) AS monto
    FROM x
    WHERE fecha_eff IS NOT NULL AND TRIM(CAST(fecha_eff AS TEXT)) <> ''
      AND CAST(fecha_eff AS TEXT) >= ? AND CAST(fecha_eff AS TEXT) <= ?
    GROUP BY TRIM(cuenta)
    """
    bal = run_query(q, db_name, params=[start_iso, end_iso])
    if bal is None or bal.empty:
        return 0.0
    m = cf.merge(bal, how="left", on="cuenta")
    m["monto"] = pd.to_numeric(m["monto"], errors="coerce").fillna(0.0)
    return float(m["monto"].sum())


def copy_text_to_windows_clipboard(text: str) -> bool:
    try:
        subprocess.run(["clip"], input=str(text), text=True, check=True, shell=True)
        return True
    except Exception:
        return False


def clean_path(path):
    """Limpia rutas de archivos arrastradas desde PowerShell/Windows."""
    path = path.strip()
    if path.startswith('& '):
        path = path[2:].strip()
    while (path.startswith("'") and path.endswith("'")) or (path.startswith('"') and path.endswith('"')):
        path = path[1:-1].strip()
    return path


def _load_clasificacion() -> pd.DataFrame:
    """Carga y normaliza la clasificación desde `clasificacion.xlsx`."""
    global _CLASIF_CACHE
    if _CLASIF_CACHE is not None:
        return _CLASIF_CACHE
    path = Path("clasificacion.xlsx")
    if not path.exists():
        _CLASIF_CACHE = pd.DataFrame(columns=["cuenta", "Reporte", "Detalle N1", "Detalle N2"])
        return _CLASIF_CACHE
    df = pd.read_excel(path)
    if "Cuenta Descripcion" in df.columns and "cuenta" not in df.columns:
        raw = df["Cuenta Descripcion"].astype(str).fillna("")
        df["cuenta"] = raw.str.extract(r"^\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", expand=False).fillna("")
        if (df["cuenta"] == "").all():
            df["cuenta"] = raw.str.split().str[0].fillna("")
    keep = [c for c in ["cuenta", "Reporte", "Detalle N1", "Detalle N2"] if c in df.columns]
    df = df[keep].copy() if keep else pd.DataFrame(columns=["cuenta", "Reporte", "Detalle N1", "Detalle N2"])
    df["cuenta"] = df.get("cuenta", "").astype(str).str.strip()
    df["Reporte"] = df.get("Reporte", "").astype(str).str.strip().str.upper()
    df["Detalle N1"] = df.get("Detalle N1", "").astype(str).str.strip()
    df["Detalle N2"] = df.get("Detalle N2", "").astype(str).str.strip()
    _CLASIF_CACHE = df
    return df


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    """Devuelve (inicio_iso, fin_iso) inclusivo para el mes dado."""
    from datetime import date

    start = date(year, month, 1)
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    end = next_month.fromordinal(next_month.toordinal() - 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _available_periods(db_name: str) -> list[tuple[int, int]]:
    """Lista meses disponibles en ledger_runtime como (YYYY, MM)."""
    try:
        df = run_query(
            """
            SELECT DISTINCT SUBSTR(fecha, 1, 4) AS y, SUBSTR(fecha, 6, 2) AS m
            FROM ledger_runtime
            WHERE COALESCE(TRIM(fecha), '') <> ''
            ORDER BY y DESC, m DESC
            """,
            db_name,
        )
        if df is None or df.empty:
            return []
        out: list[tuple[int, int]] = []
        for _, r in df.iterrows():
            try:
                y = int(str(r.get("y", "")).strip())
                m = int(str(r.get("m", "")).strip())
                if 1900 < y < 2100 and 1 <= m <= 12:
                    out.append((y, m))
            except Exception:
                continue
        return out
    except Exception:
        return []


def _build_libro_ventas_layout(df_docs: pd.DataFrame, y: int, m: int, empresa: str, rut: str) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """
    Construye 2 tablas estilo Kame:
    - Detalle principal con subtotales por tipo_doc
    - Resumen general libro
    """
    work = df_docs.copy()
    # Normaliza columnas esperadas
    for c in ["exento", "neto", "iva", "otros", "total"]:
        if c not in work.columns:
            work[c] = 0.0
        work[c] = pd.to_numeric(work[c], errors="coerce").fillna(0.0)
    if "tipo_doc" not in work.columns:
        work["tipo_doc"] = "OTRO"
    work["tipo_doc"] = work["tipo_doc"].astype(str).str.strip().replace({"": "OTRO"})

    # Formato fecha DD/MM/YYYY para el layout
    fecha_dt = pd.to_datetime(work.get("fecha"), errors="coerce")
    work["FECHA"] = fecha_dt.dt.strftime("%d/%m/%Y").fillna(work.get("fecha").astype(str))
    work["NÚMERO"] = work.get("folio", "").astype(str).str.extract(r"([0-9]+)", expand=False).fillna(work.get("folio", ""))
    work["R.U.T"] = work.get("rut_ficha", "").astype(str)
    work["NOMBRE"] = work.get("razon_social", "").astype(str)
    work["EXENTO"] = work["exento"]
    work["NETO"] = work["neto"]
    work["IVA"] = work["iva"]
    work["OTROS"] = work["otros"]
    work["TOTAL"] = work["total"]

    detail_cols = ["FECHA", "NÚMERO", "R.U.T", "NOMBRE", "EXENTO", "NETO", "IVA", "OTROS", "TOTAL"]
    rows = []
    for tipo, g in work.sort_values(["tipo_doc", "FECHA", "NÚMERO"]).groupby("tipo_doc", sort=False):
        rows.append({detail_cols[0]: f"[b]{tipo}[/]"})
        for _, r in g.iterrows():
            rows.append(
                {
                    "FECHA": r["FECHA"],
                    "NÚMERO": r["NÚMERO"],
                    "R.U.T": r["R.U.T"],
                    "NOMBRE": r["NOMBRE"],
                    "EXENTO": r["EXENTO"],
                    "NETO": r["NETO"],
                    "IVA": r["IVA"],
                    "OTROS": r["OTROS"],
                    "TOTAL": r["TOTAL"],
                }
            )
        rows.append(
            {
                "FECHA": f"[b]SUBTOTAL {tipo}[/]",
                "EXENTO": float(g["exento"].sum()),
                "NETO": float(g["neto"].sum()),
                "IVA": float(g["iva"].sum()),
                "OTROS": float(g["otros"].sum()),
                "TOTAL": float(g["total"].sum()),
            }
        )
        rows.append({})  # separador

    # Totales generales
    rows.append(
        {
            "FECHA": "[b]TOTAL GENERAL[/]",
            "EXENTO": float(work["exento"].sum()),
            "NETO": float(work["neto"].sum()),
            "IVA": float(work["iva"].sum()),
            "OTROS": float(work["otros"].sum()),
            "TOTAL": float(work["total"].sum()),
        }
    )

    df_layout = pd.DataFrame(rows, columns=detail_cols).fillna("")

    # Resumen general
    res = (
        work.groupby("tipo_doc", as_index=False)
        .agg(
            cantidad_docs=("tipo_doc", "size"),
            total_exento=("exento", "sum"),
            total_neto=("neto", "sum"),
            total_iva=("iva", "sum"),
            total_otros=("otros", "sum"),
            total=("total", "sum"),
        )
        .sort_values(["tipo_doc"])
        .reset_index(drop=True)
    )
    total_row = pd.DataFrame(
        [
            {
                "tipo_doc": "TOTAL GENERAL",
                "cantidad_docs": int(work.shape[0]),
                "total_exento": float(work["exento"].sum()),
                "total_neto": float(work["neto"].sum()),
                "total_iva": float(work["iva"].sum()),
                "total_otros": float(work["otros"].sum()),
                "total": float(work["total"].sum()),
            }
        ]
    )
    df_resumen = pd.concat([res, total_row], ignore_index=True)

    summary = (
        f"LIBRO DE VENTAS | {m:02d}-{y} | DOCS: {len(work)} | NETO: {format_miles(work['neto'].sum())} | "
        f"IVA: {format_miles(work['iva'].sum())} | TOTAL: {format_miles(work['total'].sum())}"
    )
    return df_layout, df_resumen, summary

def set_db_metadata(db_name, nombre, rut):
    """Guarda el nombre y RUT de la empresa en la base de datos."""
    import sqlite3
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS metadata (empresa_nombre TEXT, empresa_rut TEXT)")
    cursor.execute("DELETE FROM metadata")
    cursor.execute("INSERT INTO metadata (empresa_nombre, empresa_rut) VALUES (?, ?)", (nombre, rut))
    conn.commit()
    conn.close()

def get_known_companies():
    """Busca en todas las DBs disponibles para crear un catálogo de empresas conocidas."""
    companies = {} # rut -> nombre
    db_files = [f for f in os.listdir('.') if f.endswith('_kame.db')]
    for db in db_files:
        try:
            import sqlite3
            conn = sqlite3.connect(db)
            cursor = conn.cursor()
            cursor.execute("SELECT empresa_nombre, empresa_rut FROM metadata LIMIT 1")
            row = cursor.fetchone()
            if row: companies[row[1]] = row[0]
            conn.close()
        except: pass
    return companies

class ImportScreen(ModalScreen):
    """Pantalla para importar nuevos archivos Excel."""
    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("IMPORTAR NUEVOS DATOS (LEDGER/EXCEL)", id="import-title"),
            Label("Ruta del archivo Excel:"),
            Input(placeholder="Arrastra el archivo o escribe la ruta...", id="file-path"),
            Label("RUT de la Empresa:"),
            Input(placeholder="12.345.678-9", id="rut-input"),
            Label("Nombre de la Empresa:"),
            Input(placeholder="Empresa S.A.", id="name-input"),
            Horizontal(
                Button("Procesar Ahora", variant="primary", id="btn-process"),
                Button("Cancelar", variant="error", id="btn-cancel"),
            ),
            id="import-dialog"
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "rut-input":
            rut = event.value.strip()
            known = get_known_companies()
            if rut in known:
                self.query_one("#name-input").value = known[rut]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-process":
            path_raw = self.query_one("#file-path").value
            file_path = clean_path(path_raw)
            rut = self.query_one("#rut-input").value
            nombre = self.query_one("#name-input").value
            
            if not os.path.exists(file_path):
                self.app.notify("El archivo no existe", severity="error")
                return
            if not rut or not nombre:
                self.app.notify("RUT y Nombre son obligatorios", severity="warning")
                return
            db_name = _rut_to_kame_db_name(rut)
            if db_name == "empresa_kame.db":
                self.app.notify("RUT inválido. Revise el formato.", severity="warning")
                return

            self.app.notify("Procesando... esto puede tardar unos segundos.")
            try:
                from app.parsers.kame import read_kame_excel
                
                df = read_kame_excel(file_path)
                save_to_ledger(df, db_name)
                # Refresca libro de ventas materializado para reportes (desde ledger importado).
                try:
                    refresh_libro_ventas_from_ledger(db_name, "clasificacion.xlsx")
                except Exception:
                    pass
                set_db_metadata(db_name, nombre, rut)
                
                self.app.notify(f"Éxito: {nombre} registrado.", severity="success")
                self.app.switch_screen(CompanySelect())
            except Exception as e:
                self.app.notify(f"Error: {e}", severity="error")
        else:
            self.app.pop_screen()

class CustomHeader(Static):
    """Cabecera personalizada para evitar fallos del widget Header oficial."""
    def compose(self) -> ComposeResult:
        empresa = session["empresa"] if session["db_name"] else "SIN EMPRESA"
        user = session["user"] or "SIN LOGIN"
        yield Label(f" PY-ANALYTICA CFO | {user} | {empresa}")

class LoginScreen(ModalScreen):
    # ... (LoginScreen definitions remain the same)
    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("PY-ANALYTICA - ACCESO CFO", id="login-title"),
            Input(placeholder="Usuario (Presiona Enter para saltar)", id="username"),
            Input(placeholder="Contraseña", password=True, id="password"),
            Button("Entrar", variant="primary", id="login-btn"),
            id="login-dialog"
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.perform_login()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.perform_login()

    def perform_login(self) -> None:
        user = self.query_one("#username").value
        pwd = self.query_one("#password").value
        
        if verify_login(user, pwd):
            session["user"] = user
            self.app.switch_screen(CompanySelect())
        else:
            self.app.notify("Usuario o contraseña incorrectos", severity="error")

class UserManagementScreen(ModalScreen):
    """Pantalla para administrar usuarios (Crear/Eliminar)."""
    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("GESTIÓN DE USUARIOS", id="user-title"),
            DataTable(id="user-table"),
            Label("Añadir Nuevo Usuario:"),
            Horizontal(
                Input(placeholder="Usuario", id="new-username"),
                Input(placeholder="Password", password=True, id="new-password"),
            ),
            Horizontal(
                Button("Añadir", variant="primary", id="btn-add-user"),
                Button("Eliminar Seleccionado", variant="error", id="btn-del-user"),
                Button("Cerrar", id="btn-close-user"),
            ),
            id="user-dialog"
        )

    def on_mount(self) -> None:
        self.refresh_users()

    def refresh_users(self):
        table = self.query_one("#user-table")
        table.clear(columns=True)
        table.add_columns("Usuario")
        for u in get_all_users():
            table.add_row(u)
        table.cursor_type = "row"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-add-user":
            user = self.query_one("#new-username").value
            pwd = self.query_one("#new-password").value
            if user and pwd:
                if create_user(user, pwd):
                    self.app.notify(f"Usuario {user} creado")
                    self.refresh_users()
                else:
                    self.app.notify("Error: El usuario ya existe", severity="error")
        elif event.button.id == "btn-del-user":
            table = self.query_one("#user-table")
            try:
                row = table.get_row_at(table.cursor_row)
                target = row[0]
                if target == session["user"]:
                    self.app.notify("No puedes eliminarte a ti mismo", severity="warning")
                else:
                    delete_user(target)
                    self.app.notify(f"Usuario {target} eliminado")
                    self.refresh_users()
            except:
                self.app.notify("Selecciona un usuario primero", severity="warning")
        else:
            self.app.pop_screen()

class CompanySelect(ModalScreen):
    """Diálogo de Selección de Empresa (F2)."""
    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("SELECCIONAR EMPRESA / BASE DE DATOS", id="select-title"),
            DataTable(id="company-table"),
            Horizontal(
                Button("Seleccionar", variant="primary", id="btn-confirm"),
                Button("Importar Nuevo", variant="warning", id="btn-import"),
                Button("Cancelar", variant="error", id="btn-cancel"),
            ),
            id="select-dialog"
        )

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("ID", "Empresa", "RUT", "Archivo")
        table.cursor_type = "row"
        db_files = sorted([f for f in os.listdir('.') if f.endswith('_kame.db')], reverse=True)

        for i, f in enumerate(db_files):
            nombre, rut = self.app.get_metadata(f)
            table.add_row(str(i+1), nombre or "Nueva", rut or "-", f)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-confirm":
            table = self.query_one(DataTable)
            if table.row_count == 0:
                self.app.notify("No hay bases de datos. Importe una primero.", severity="warning")
                return
            try:
                row = table.get_row_at(table.cursor_row)
                session["db_name"] = row[3]
                session["empresa"] = row[1]
                session["rut"] = row[2]
                self.app.refresh_header()
            except:
                self.app.notify("Selección inválida", severity="error")
        elif event.button.id == "btn-import":
            self.app.push_screen(ImportScreen())
        else:
            self.app.pop_screen()


class AccountReviewModal(ModalScreen):
    """Registro de OK / nota por cuenta en balance."""

    def __init__(self, cuenta, existing=None, on_saved=None, fecha_balance=None, **kwargs):
        super().__init__(**kwargs)
        self.cuenta = str(cuenta or "").strip()
        self.existing = existing or {}
        self.on_saved = on_saved
        self.fecha_balance = str(fecha_balance or "").strip()

    def compose(self) -> ComposeResult:
        aprobado = "si" if int(self.existing.get("aprobado", 0) or 0) == 1 else "no"
        nota = str(self.existing.get("nota", "") or "")
        yield Vertical(
            Label(f"OK / NOTA CUENTA {self.cuenta}", id="report-title"),
            Horizontal(
                Label("Aprobada:"),
                Select(options=[("No", "no"), ("Sí", "si")], value=aprobado, id="acc-review-aprob"),
            ),
            Input(placeholder="Nota obligatoria (mínimo 2 caracteres)", value=nota, id="acc-review-note"),
            Horizontal(
                Button("Guardar", variant="success", id="btn-acc-review-save"),
                Button("Cancelar", variant="error", id="btn-acc-review-cancel"),
            ),
            id="search-dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-acc-review-cancel":
            self.app.pop_screen()
            return
        aprobado = self.query_one("#acc-review-aprob", Select).value == "si"
        nota = self.query_one("#acc-review-note", Input).value.strip()
        if len(nota) < 2:
            self.app.notify("La nota es obligatoria (mínimo 2 caracteres).", severity="warning")
            return
        save_account_review(
            session.get("db_name"),
            self.cuenta,
            aprobado=aprobado,
            nota=nota,
            usuario=session.get("user"),
            fecha_balance=self.fecha_balance,
        )
        if callable(self.on_saved):
            self.on_saved(aprobado, nota)
        self.app.pop_screen()
        self.app.notify(f"Cuenta {self.cuenta} guardada.", severity="success")


class ReportPreview(Screen):
    """Vista previa del reporte con exportación, drill-down y herramientas de cierre."""

    BINDINGS = [
        Binding("f5", "export", "Exportar (F5)", show=True),
        Binding("f6", "review_account", "OK/Nota (F6)", show=True),
        Binding("f7", "monthly_summary", "Resumen Mes (F7)", show=True),
        Binding("f8", "secondary", "Vista secundaria (F8)", show=True),
        Binding("ctrl+j", "aux_pivot", "Resumen Ajuste (Ctrl+J)", show=True),
        Binding("pagedown", "load_more", "Cargar más", show=False),
        Binding("ctrl+c", "copy_table", "Copiar tabla", show=True),
        Binding("escape", "back", "Volver", show=True),
    ]

    def __init__(self, title, df, export_func, summary_text="", secondary=None, **kwargs):
        super().__init__(**kwargs)
        self.title_text = title
        self.df = df
        self.export_func = export_func
        self.summary_text = summary_text
        # secondary: (title, df, export_func, summary_text)
        self.secondary = secondary
        self.render_batch_size = 1500
        self.render_trigger_threshold = 120
        self.max_render_rows = 100000
        self._open_detail_in_progress = False
        self._truncation_warned = False
        self._progressive_hint_shown = False
        self._view_df = None
        self._visible_cols = []
        self._col_index = {}
        self._loaded_rows = 0
        self._is_comprobante_view = "comprobante" in str(self.title_text).lower()
        self._base_summary_text = str(summary_text or "")

    def compose(self) -> ComposeResult:
        actions = [Button("EXPORTAR A EXCEL (F5)", variant="success", id="btn-export")]
        if not self._is_comprobante_view:
            actions.append(Button("OK / NOTA (F6)", variant="primary", id="btn-review"))
        actions.append(Button("RESUMEN MES (F7)", variant="primary", id="btn-monthly"))
        if self.secondary is not None:
            actions.append(Button("RESUMEN GENERAL (F8)", variant="primary", id="btn-secondary"))
        if not self._is_comprobante_view:
            actions.append(Button("RESUMEN AJUSTE (CTRL+J)", variant="primary", id="btn-aux-pivot"))
        actions.extend(
            [
                Button("COPIAR TABLA (CTRL+C)", variant="warning", id="btn-copy"),
                Button("VOLVER (ESC)", variant="error", id="btn-back"),
            ]
        )
        yield CustomHeader(id="main-header")
        yield Vertical(
            Label(self.title_text, id="report-title"),
            Label(self.summary_text, id="report-summary"),
            DataTable(id="report-table", cursor_type="row"),
            Horizontal(*actions, id="report-actions"),
            id="preview-content",
        )
        yield Footer()

    def action_back(self) -> None:
        self.app.pop_screen()

    def on_mount(self) -> None:
        self._render_table()

    def _render_table(self, keep_cursor_row: int | None = None) -> None:
        table = self.query_one(DataTable)
        table.zebra_stripes = True
        table.clear(columns=True)
        if self.df is None or self.df.empty:
            table.focus()
            return

        hidden = {"saldo", "tiene_auxiliar"}
        if "balance" in self.title_text.lower():
            hidden.add("aprobado")
        self._visible_cols = [
            c for c in self.df.columns if not c.startswith("_") and c.lower() not in hidden
        ]
        for col in self._visible_cols:
            table.add_column(col)

        self._view_df = self.df.head(self.max_render_rows) if len(self.df) > self.max_render_rows else self.df
        self._col_index = {c: i for i, c in enumerate(self._view_df.columns)}
        self._loaded_rows = 0
        self._append_rows(self.render_batch_size)

        if len(self.df) > self.max_render_rows and not self._truncation_warned:
            self._truncation_warned = True
            self.notify(
                f"Vista UI limitada a {self.max_render_rows} filas. Exportar/Copiar usa todos los datos.",
                severity="warning",
            )
        if len(self._view_df) > self._loaded_rows and not self._progressive_hint_shown:
            self._progressive_hint_shown = True
            self.notify("Scroll dinámico: al bajar se cargan más filas.", severity="information")

        if keep_cursor_row is not None and table.row_count > 0:
            if keep_cursor_row >= self._loaded_rows:
                self._append_rows(max(self.render_batch_size, keep_cursor_row - self._loaded_rows + 1))
            target_row = max(0, min(keep_cursor_row, table.row_count - 1))
            table.move_cursor(row=target_row, animate=False, scroll=True)
        table.focus()

    def _append_rows(self, rows_to_add: int) -> None:
        if self._view_df is None or rows_to_add <= 0:
            return
        if self._loaded_rows >= len(self._view_df):
            return
        if not self._col_index or not self._visible_cols:
            return
        table = self.query_one(DataTable)
        start = self._loaded_rows
        end = min(len(self._view_df), start + rows_to_add)
        idx = self._col_index
        slice_df = self._view_df.iloc[start:end]

        for abs_row_idx, row in enumerate(slice_df.itertuples(index=False, name=None), start=start):
            formatted_row = []
            row_is_auxiliar = (
                str(row[idx["tiene_auxiliar"]]).strip() == "1" if "tiene_auxiliar" in idx else False
            )
            row_aprobada = str(row[idx["ok"]]).strip().upper() == "X" if "ok" in idx else False

            row_style = ""
            if "_style" in idx:
                row_style = str(row[idx["_style"]] or "").strip().lower()

            for col in self._visible_cols:
                val = row[idx[col]]
                max_name = 48 if "INFORME" in str(self.title_text).upper() else 25
                if col == "nombre_cuenta" and len(str(val)) > max_name:
                    val = str(val)[: max_name - 3] + "..."

                cl = col.lower().strip()
                if cl in [
                    "debe",
                    "haber",
                    "activo",
                    "pasivo",
                    "perdida",
                    "ganancia",
                    "saldo",
                    "saldo pendiente",
                    "monto pendiente",
                    "monto",
                    "acumulado",
                ]:
                    val_str = format_miles_contable(val)
                elif cl == "pct":
                    val_str = "-" if val is None or str(val).strip() == "" else str(val)
                elif val is None or str(val).lower() == "none":
                    val_str = "-"
                else:
                    val_str = str(val)

                styled_cols = ("concepto", "acumulado", "pct", "detalle n1", "detalle n2", "clasificacion")
                styled_cell = cl in styled_cols or not str(col).strip()
                if row_style == "blue" and styled_cell:
                    val_str = f"[b white on #1e40af]{val_str}[/]"
                elif row_style == "dark" and styled_cell:
                    val_str = f"[b white on #14532d]{val_str}[/]"

                if row_aprobada:
                    formatted_row.append(f"[b white on #145A32]{val_str}[/]")
                elif row_is_auxiliar:
                    formatted_row.append(f"[b green]{val_str}[/]")
                else:
                    formatted_row.append(val_str)

            table.add_row(*formatted_row)
        self._loaded_rows = end

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self._view_df is None or self._loaded_rows >= len(self._view_df):
            return
        if event.cursor_row >= (self._loaded_rows - self.render_trigger_threshold):
            self._append_rows(self.render_batch_size)

    def action_load_more(self) -> None:
        before = self._loaded_rows
        self._append_rows(self.render_batch_size)
        if self._loaded_rows > before:
            self.notify(f"Filas cargadas: {self._loaded_rows}/{len(self._view_df)}", severity="information")

    def action_export(self) -> None:
        self.app.notify("Generando Excel en segundo plano...", severity="information")

        def do_export():
            try:
                filename = self.export_func()
                if filename:
                    self.app.call_from_thread(
                        self.app.notify, f"Excel generado: {filename}", severity="success"
                    )
                else:
                    self.app.call_from_thread(
                        self.app.notify, "No hay datos para exportar", severity="warning"
                    )
            except Exception as e:
                self.app.call_from_thread(
                    self.app.notify, f"Error al exportar: {str(e)}", severity="error"
                )

        self.run_worker(do_export, thread=True)

    def action_secondary(self) -> None:
        if self.secondary is None:
            self.notify("No hay vista secundaria disponible.", severity="warning")
            return
        t, df2, exp2, summ2 = self.secondary
        self.app.push_screen(ReportPreview(t, df2, exp2, summ2))

    def action_copy_table(self) -> None:
        if self.df is None or self.df.empty:
            self.notify("No hay datos para copiar.", severity="warning")
            return
        cols = [
            c
            for c in self.df.columns
            if not c.startswith("_") and c.lower() not in ["saldo", "tiene_auxiliar", "aprobado"]
        ]
        df_copy = self.df[cols].copy()
        numeric_money_cols = {
            "debe",
            "haber",
            "activo",
            "pasivo",
            "perdida",
            "ganancia",
            "saldo",
            "saldo pendiente",
            "monto pendiente",
            "monto",
            "total",
            "acumulado",
        }
        for col in list(df_copy.columns):
            if str(col).strip().lower() in numeric_money_cols:
                vals = pd.to_numeric(df_copy[col], errors="coerce")
                df_copy[col] = vals.fillna(0).round(0).astype("int64").astype(str)
        if copy_text_to_windows_clipboard(df_copy.to_csv(sep="\t", index=False, lineterminator="\n")):
            self.notify("Tabla copiada al portapapeles.", severity="success")
        else:
            self.notify("No se pudo copiar al portapapeles.", severity="error")

    def action_monthly_summary(self) -> None:
        if self.df is None or self.df.empty:
            self.notify("No hay datos para resumir.", severity="warning")
            return
        # Libro de Ventas: el resumen mensual no aplica; dejamos la tabla tal cual.
        if "LIBRO DE VENTAS" in str(self.title_text).upper():
            self.notify("Resumen mensual no aplica para Libro de Ventas.", severity="warning")
            return
        if not {"fecha", "debe", "haber"}.issubset(set(self.df.columns)):
            self.notify("Faltan columnas para resumen mensual.", severity="warning")
            return

        work = self.df.copy()
        fecha_dt = pd.to_datetime(work["fecha"], errors="coerce", dayfirst=True)
        work["debe"] = pd.to_numeric(work["debe"], errors="coerce").fillna(0.0)
        work["haber"] = pd.to_numeric(work["haber"], errors="coerce").fillna(0.0)

        concepto = (
            work.get("concepto", "").astype(str).str.upper()
            if "concepto" in work.columns
            else pd.Series([""] * len(work))
        )
        comprobante = (
            work.get("comprobante", "").astype(str).str.upper()
            if "comprobante" in work.columns
            else pd.Series([""] * len(work))
        )
        is_apertura = concepto.str.contains("APERTURA", na=False) | comprobante.str.startswith(
            "I0", na=False
        )

        work["mes_num"] = fecha_dt.dt.month.fillna(0).astype(int)
        work.loc[is_apertura, "mes_num"] = 0
        work["mes"] = work["mes_num"].apply(lambda m: "00-APERTURA" if int(m) == 0 else f"{int(m):02d}")

        df_month = (
            work.groupby(["mes_num", "mes"], as_index=False)[["debe", "haber"]]
            .sum()
            .sort_values(["mes_num"])
        )
        df_month["monto"] = df_month["debe"] - df_month["haber"]
        df_month = df_month[["mes", "debe", "haber", "monto"]]

        summary = (
            f"MESES: {len(df_month)} | "
            f"DEBE: {format_miles(df_month['debe'].sum())} | "
            f"HABER: {format_miles(df_month['haber'].sum())} | "
            f"MONTO: {format_miles(df_month['monto'].sum())}"
        )
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", self.title_text)[-40:] or "mayor"
        export_call = lambda: export_dataframe_simple_excel(
            session.get("db_name"), df_month, f"resumen_mes_mayor_{safe}", sheet_name="resumen_mes"
        )
        self.app.push_screen(ReportPreview(f"RESUMEN MENSUAL - {self.title_text}", df_month, export_call, summary))

    def action_review_account(self) -> None:
        if self._is_comprobante_view:
            return
        if "balance" not in self.title_text.lower():
            self.app.notify("OK/Notas aplica desde la vista de Balance.", severity="warning")
            return
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return
        row_data = table.get_row_at(table.cursor_row)
        cols = {str(col.label.plain).lower().strip(): i for i, col in enumerate(table.columns.values())}
        cidx = cols.get("cuenta")
        if cidx is None:
            return
        cuenta = re.sub(r"\[.*?\]", "", str(row_data[cidx])).strip()
        review_date = getattr(self.app, "last_balance_date", "") or ""
        review = (
            get_account_review(session["db_name"], cuenta, fecha_balance=review_date)
            if session.get("db_name")
            else None
        )
        current_row = table.cursor_row

        def on_saved(aprobado: bool, _nota: str) -> None:
            if self.df is None or self.df.empty or "cuenta" not in self.df.columns:
                return
            mask = self.df["cuenta"].astype(str).str.strip() == str(cuenta).strip()
            if mask.any():
                self.df.loc[mask, "ok"] = "X" if aprobado else ""
                if "aprobado" in self.df.columns:
                    self.df.loc[mask, "aprobado"] = 1 if aprobado else 0
                self._render_table(keep_cursor_row=current_row)

        self.app.push_screen(AccountReviewModal(cuenta, review, on_saved=on_saved, fecha_balance=review_date))

    def action_aux_pivot(self) -> None:
        if self._is_comprobante_view:
            return
        if self.df is None or self.df.empty:
            self.notify("No hay datos para resumir.", severity="warning")
            return
        if "mayor" not in self.title_text.lower():
            self.notify("Resumen ajuste disponible desde Libro Mayor.", severity="warning")
            return
        if "cuenta" not in self.df.columns:
            self.notify("No se pudo identificar la cuenta base.", severity="warning")
            return

        cuenta_base = str(self.df["cuenta"].dropna().astype(str).iloc[0]).strip()
        has_aux_in_view = False
        doc_col = None
        for cand in ("doc_referencia", "documento"):
            if cand in self.df.columns:
                doc_col = cand
                break
        if doc_col and "ficha" in self.df.columns:
            ficha_vals = self.df["ficha"].astype(str).str.strip()
            doc_vals = self.df[doc_col].astype(str).str.strip()
            has_aux_in_view = (
                ficha_vals.notna()
                & doc_vals.notna()
                & (~ficha_vals.isin(["", "0", "None", "nan"]))
                & (~doc_vals.isin(["", "0", "-", "0 #0", "None", "nan #nan"]))
            ).any()

        if not has_aux_in_view:
            self.notify("La cuenta no muestra auxiliar/documento en el mayor. No se genera resumen.", severity="warning")
            return

        q_live = """
            SELECT
                cuenta, nombre_cuenta, ficha, razon_social,
                COALESCE(NULLIF(TRIM(documento), ''), NULLIF(TRIM(doc_referencia), ''), '') AS doc_referencia,
                concepto, debe, haber
            FROM ledger_runtime
            WHERE REPLACE(REPLACE(REPLACE(UPPER(TRIM(COALESCE(cuenta, ''))), '-', ''), '.', ''), ' ', '')
                = REPLACE(REPLACE(REPLACE(UPPER(TRIM(?)), '-', ''), '.', ''), ' ', '')
            """
        try:
            live_df = run_query(q_live, session.get("db_name"), params=[cuenta_base])
            work = live_df if live_df is not None and not live_df.empty else self.df.copy()
        except Exception:
            work = self.df.copy()

        needed = {"ficha", "debe", "haber"}
        if not needed.issubset(set(work.columns)):
            self.notify("Faltan columnas para generar resumen de ajuste.", severity="warning")
            return

        work["ficha"] = work["ficha"].astype(str).str.strip()
        work["razon_social"] = work.get("razon_social", pd.Series([""] * len(work))).astype(str).str.strip()
        if "doc_referencia" not in work.columns:
            work["doc_referencia"] = ""
        work["debe"] = pd.to_numeric(work["debe"], errors="coerce").fillna(0.0)
        work["haber"] = pd.to_numeric(work["haber"], errors="coerce").fillna(0.0)
        work["saldo"] = work["debe"] - work["haber"]

        pivot = work.groupby(["ficha", "razon_social"], as_index=False)["saldo"].sum()
        pivot = pivot[pivot["ficha"].notna() & (pivot["ficha"].str.len() > 0)]
        pivot = pivot[pivot["saldo"].abs() > 0.1]
        pivot = pivot.sort_values(["ficha", "razon_social"]).reset_index(drop=True)

        if pivot.empty:
            self.notify("No hay pendientes auxiliares para resumir en esta cuenta.", severity="warning")
            return

        df_resumen = pd.DataFrame(
            {
                "Auxiliar": pivot["ficha"],
                "razon_social": pivot["razon_social"],
                "Saldo Pendiente": pivot["saldo"],
            }
        )
        acc_name = ""
        if "nombre_cuenta" in self.df.columns:
            names = self.df["nombre_cuenta"].dropna().astype(str)
            if not names.empty:
                acc_name = names.iloc[0]
        title = f"RESUMEN AJUSTE: {cuenta_base}" + (f" - {acc_name}" if acc_name else "")
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", cuenta_base)[:30]
        export_call = lambda: export_dataframe_simple_excel(
            session.get("db_name"), df_resumen, f"resumen_ajuste_{safe}", sheet_name="resumen"
        )
        tot = format_miles(df_resumen["Saldo Pendiente"].sum())
        self.app.push_screen(
            ReportPreview(title, df_resumen, export_call, summary=f"SALDO NETO AUX: {tot}")
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self._open_detail_in_progress:
            return
        self._open_detail_in_progress = True
        self.app.notify("Abriendo detalle...", severity="information")

        table = self.query_one(DataTable)
        row_data = table.get_row_at(event.cursor_row)
        cols = {str(col.label.plain).lower().strip(): i for i, col in enumerate(table.columns.values())}

        def strip_tags(text):
            if not isinstance(text, str):
                return text
            return re.sub(r"\[.*?\]", "", text)

        try:
            acc_idx = cols.get("cuenta")
            if acc_idx is None:
                return
            cuenta = strip_tags(row_data[acc_idx]).strip()

            if "balance" in self.title_text.lower():
                tiene_aux = False
                cr = int(event.cursor_row)
                if self.df is not None and 0 <= cr < len(self.df) and "tiene_auxiliar" in self.df.columns:
                    tiene_aux = str(self.df.iloc[cr].get("tiene_auxiliar", "")).strip() in (
                        "1",
                        "True",
                        "true",
                    )
                if tiene_aux:
                    self.app.notify(f"Pendientes de documentos: {cuenta}")
                    self.app.generate_preview("pendientes_cuenta", cuenta)
                else:
                    self.app.notify(f"Libro Mayor: {cuenta}")
                    self.app.generate_preview("mayor", cuenta)
                return

            if "pendiente" in self.title_text.lower() or "documentos pendientes" in self.title_text.lower():
                f_idx = cols.get("ficha") or cols.get("auxiliar")
                d_idx = cols.get("doc_referencia") or cols.get("documento ref.") or cols.get("documento")
                if f_idx is not None and d_idx is not None:
                    ficha = strip_tags(row_data[f_idx])
                    doc = strip_tags(row_data[d_idx])
                    if ficha not in ("", "-") and doc not in ("", "-"):
                        self.app.notify(f"Histórico: {ficha} | {doc}")
                        self.app.generate_preview("historico", f"{ficha}, {doc}")
                        return

            if "ficha" in cols:
                f_idx = cols.get("ficha")
                d_idx = cols.get("documento") or cols.get("doc_referencia")
                if f_idx is not None and d_idx is not None:
                    ficha = strip_tags(row_data[f_idx])
                    doc = strip_tags(row_data[d_idx])
                    if ficha not in ("", "-") and doc not in ("", "-"):
                        self.app.notify(f"Buscando pendientes de {ficha} | {doc}")
                        self.app.generate_preview("inteligente", f"{ficha}, {doc}")
                        return

        except (ValueError, IndexError):
            pass
        finally:
            self._open_detail_in_progress = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-export":
            self.action_export()
        elif event.button.id == "btn-review":
            self.action_review_account()
        elif event.button.id == "btn-monthly":
            self.action_monthly_summary()
        elif event.button.id == "btn-secondary":
            self.action_secondary()
        elif event.button.id == "btn-aux-pivot":
            self.action_aux_pivot()
        elif event.button.id == "btn-copy":
            self.action_copy_table()
        else:
            self.app.pop_screen()


class SearchModal(ModalScreen):
    def __init__(self, tipo_analisis, callback, **kwargs):
        super().__init__(**kwargs)
        self.tipo_analisis = tipo_analisis
        self.callback = callback

    def compose(self) -> ComposeResult:
        if self.tipo_analisis == "balance":
            msg = "Fecha de Corte (DD-MM-AAAA)"
            default = datetime.now().strftime("%d-%m-%Y")
        elif self.tipo_analisis == "relacion_grupos":
            msg = "Filtro (texto o grupo, ej: proveedores o 2-1-*)"
            default = "*"
        else:
            msg = "¿Qué buscas? (Nombre/RUT/Concepto o '*')"
            default = "*"

        body = [
            Label(f"FILTRAR REPORTE: {self.tipo_analisis.upper()}"),
            Input(placeholder=msg, value=default, id="search-input"),
        ]
        if self.tipo_analisis == "balance":
            body.append(
                Checkbox("Solo cuentas sin aprobar (esta fecha de corte)", value=False, id="chk-solo-no-aprob")
            )
        body.append(
            Horizontal(
                Button("Ver en Pantalla", variant="primary", id="btn-view"),
                Button("Cancelar", variant="error", id="btn-cancel"),
            )
        )
        yield Vertical(*body, id="search-dialog")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-view":
            value = self.query_one("#search-input").value
            self.app.pop_screen()
            if self.tipo_analisis == "balance":
                solo = self.query_one("#chk-solo-no-aprob", Checkbox).value
                self.callback({"fecha": value, "solo_no_aprobadas": bool(solo)})
            else:
                self.callback(value)
        else:
            self.app.pop_screen()

class Dashboard(Screen):
    def compose(self) -> ComposeResult:
        yield CustomHeader(id="main-header")
        yield Container(
            Vertical(
                Label("PANEL DE SUPERVISIÓN CFO", id="dash-title"),
                Grid(
                    Button("Análisis Inteligente", id="report-inteligente"),
                    Button("Histórico Completo", id="report-historico"),
                    Button("Libro Mayor", id="report-mayor"),
                    Button("Balance 8 Col", id="report-balance"),
                    Button("Comprobante", id="report-comprobante"),
                    Button("Notas Cuentas", id="report-notas_cuentas"),
                    Button("Relación Conceptos/Grupos", id="report-relacion_grupos"),
                    Button("Informes Gestión", id="report-informes"),
                    id="dash-grid"
                ),
                id="dash-content"
            ),
            id="dash-container"
        )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "report-notas_cuentas":
            self.app.generate_preview("notas_cuentas", "*")
            return
        if event.button.id == "report-informes":
            self.app.push_screen(InformesMenuScreen())
            return
        tipo = event.button.id.replace("report-", "")
        self.app.push_screen(SearchModal(tipo, lambda v: self.app.generate_preview(tipo, v)))


class InformesMenuScreen(Screen):
    """Menú de Informes de Gestión (mensual) basado en clasificación."""

    BINDINGS = [
        Binding("escape", "back", "Volver", show=True),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.periods: list[tuple[int, int]] = []

    def compose(self) -> ComposeResult:
        yield CustomHeader(id="main-header")
        yield Vertical(
            Label("INFORMES DE GESTIÓN (MENSUAL)", id="report-title"),
            Label("Selecciona período (mes) y luego el informe.", id="report-summary"),
            Horizontal(
                Label("Período:"),
                Select(options=[("Cargando...", "loading")], value="loading", id="inf-period"),
            ),
            Horizontal(
                Button("Estado de Situación (EESS)", variant="primary", id="inf-eess"),
                Button("Estado de Resultado (PYG)", variant="primary", id="inf-pyg"),
            ),
            Horizontal(
                Button("Libro de Ventas vs Facturas por Emitir", id="inf-ventas", variant="warning"),
                Button("Distribución Gastos Operativos (asiento)", id="inf-dist-op", variant="warning"),
            ),
            Horizontal(
                Button("Volver", variant="error", id="inf-back"),
            ),
            id="informes-menu-content",
        )
        yield Footer()

    def action_back(self) -> None:
        self.app.pop_screen()

    def on_mount(self) -> None:
        db_name = session.get("db_name")
        if not db_name:
            self.app.notify("Primero seleccione una empresa (F2)", severity="warning")
            self.app.pop_screen()
            return
        self.periods = _available_periods(db_name)
        if not self.periods:
            self.app.notify("No hay periodos disponibles. Importa datos KAME primero.", severity="warning")
            return
        options = [(f"{m:02d}-{y}", f"{y}-{m:02d}") for (y, m) in self.periods]
        sel = self.query_one("#inf-period", Select)
        sel.set_options(options)
        sel.value = options[0][1]

    def _selected_period(self) -> tuple[int, int] | None:
        sel = self.query_one("#inf-period", Select).value
        if not sel or sel == "loading":
            return None
        try:
            y_s, m_s = str(sel).split("-")
            y, m = int(y_s), int(m_s)
            return y, m
        except Exception:
            return None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in {"inf-back"}:
            self.app.pop_screen()
            return
        period = self._selected_period()
        if not period:
            self.app.notify("Seleccione un período válido.", severity="warning")
            return
        y, m = period
        payload = {"year": y, "month": m}
        if event.button.id == "inf-eess":
            self.app.generate_preview("informe_eess", payload)
        elif event.button.id == "inf-pyg":
            self.app.generate_preview("informe_pyg", payload)
        elif event.button.id == "inf-ventas":
            self.app.generate_preview("libro_ventas", payload)
        elif event.button.id == "inf-dist-op":
            self.app.notify("Pendiente: distribución gastos operativos y asiento Kame.", severity="warning")


class KamePremium(App):
    # CSS actualizado para nuevos diálogos
    CSS = """
    Screen {
        background: #1e1e1e;
        align: center middle;
    }
    #main-header {
        background: $primary-darken-2;
        color: white;
        text-style: bold;
        padding: 0 1;
        height: 1;
        dock: top;
    }
    #login-dialog, #erp-dialog, #select-dialog, #search-dialog, #user-dialog {
        width: 65;
        height: auto;
        background: #2c3e50;
        border: thick $primary;
        padding: 1;
    }
    #user-table {
        height: 10;
        margin: 1 0;
    }
    #dash-container {
        width: 100%;
        height: 100%;
        align: center middle;
    }
    #dash-content {
        width: 80;
        height: auto;
        background: #2c3e50;
        border: panel $primary;
        padding: 1;
    }
    #dash-grid {
        grid-size: 2;
        grid-gutter: 1;
        padding: 1;
        height: auto;
    }
    #import-dialog {
        width: 70;
        height: auto;
        background: #2c3e50;
        border: thick $warning;
        padding: 1;
    }
    #import-title {
        background: $warning;
        color: black;
        text-style: bold;
    }
    #dash-title, #report-title, #login-title, #erp-title, #select-title, #import-title {
        text-align: center;
        width: 100%;
        padding: 1;
        margin-bottom: 1;
    }
    #dash-title, #report-title, #login-title, #erp-title, #select-title {
        color: white;
        background: $primary;
    }
    #report-summary {
        background: #34495e;
        color: #f1c40f;
        text-align: center;
        padding: 0 1;
        height: auto;
        border-bottom: solid $primary;
    }
    #report-table {
        height: 1fr;
    }
    #report-actions {
        height: 5;
        align: center middle;
        background: $boost;
        margin: 1 0;
    }
    #report-actions Button {
        margin: 0 3;
        min-width: 22;
        height: 3;
    }
    #informes-menu-content {
        width: 90;
        height: auto;
        padding: 1 2;
    }
    #informes-menu-content Horizontal {
        margin: 1 0;
        height: auto;
    }
    #informes-menu-content Button {
        margin: 0 2;
        min-width: 28;
        height: 3;
    }
    Footer {
        dock: bottom;
    }
    """
    last_balance_date = None

    BINDINGS = [
        Binding("f2", "select_company", "Empresa (F2)", show=True),
        Binding("f9", "manage_users", "Usuarios (F9)", show=True),
        Binding("f10", "quit", "Salir", show=True),
        Binding("escape", "back", "Volver", show=False),
    ]

    def action_manage_users(self) -> None:
        if session["user"]:
            self.push_screen(UserManagementScreen())

    def on_mount(self) -> None:
        setup_users_db()
        self.push_screen(LoginScreen())

    def action_select_company(self) -> None:
        self.push_screen(CompanySelect())

    def action_back(self) -> None:
        if len(self.screen_stack) > 1:
            self.pop_screen()

    def refresh_header(self) -> None:
        self.switch_screen(Dashboard())
        self.notify(f"Sesión iniciada: {session['user']} | {session['empresa']}")

    def generate_preview(self, tipo, busca):
        db_name = session["db_name"]
        if not db_name: 
            self.notify("Primero seleccione una empresa (F2)", severity="warning")
            return
        runtime_ok = ensure_runtime_objects(db_name)
        if not runtime_ok:
            self.notify("La base no tiene ledger. Importa datos KAME antes de generar reportes.", severity="warning")
            return
        ensure_account_reviews_schema(db_name)

        self.notify(f"Generando vista previa de {tipo}...")
        df = None
        title = (
            f"VISTA PREVIA: {tipo.upper()} - {busca.get('fecha', busca)}"
            if isinstance(busca, dict)
            else f"VISTA PREVIA: {tipo.upper()} - {busca}"
        )
        summary = ""

        sql_fecha = "COALESCE(l.fecha, (SELECT fecha FROM ledger_runtime l2 WHERE l2.comprobante = l.comprobante AND l2.fecha IS NOT NULL LIMIT 1))"

        try:
            if tipo == "mayor":
                query = f"""
                SELECT 
                    {sql_fecha} as fecha, l.comprobante, l.cuenta, l.nombre_cuenta, l.ficha, l.razon_social,
                    l.documento, l.doc_referencia, l.debe, l.haber, l.concepto 
                FROM ledger_runtime l 
                WHERE (UPPER(l.nombre_cuenta) LIKE ? OR UPPER(l.cuenta) LIKE ?) 
                ORDER BY l.cuenta, fecha, l.comprobante
                """
                p = f"%{str(busca).upper()}%"
                df = run_query(query, db_name, params=[p, p])
                if not df.empty:
                    df["debe"] = pd.to_numeric(df["debe"], errors="coerce").fillna(0.0)
                    df["haber"] = pd.to_numeric(df["haber"], errors="coerce").fillna(0.0)
                    fecha_raw = df["fecha"].astype(str)
                    fecha_dt = pd.to_datetime(df["fecha"], errors="coerce")
                    df["fecha"] = fecha_dt.dt.strftime("%d-%m-%Y")
                    df.loc[fecha_dt.isna(), "fecha"] = fecha_raw[fecha_dt.isna()]
                    summary = (
                        f"TOTAL DEBE: {format_miles(df['debe'].sum())} | TOTAL HABER: {format_miles(df['haber'].sum())} | "
                        f"SALDO: {format_miles(df['debe'].sum() - df['haber'].sum())}"
                    )
                export_call = lambda: export_libro_mayor(db_name, busca)

            elif tipo == "balance":
                solo_no_aprobadas = False
                if isinstance(busca, dict):
                    solo_no_aprobadas = bool(busca.get("solo_no_aprobadas"))
                    busca = str(busca.get("fecha") or datetime.now().strftime("%d-%m-%Y"))
                try:
                    fecha_db = datetime.strptime(busca, "%d-%m-%Y").strftime("%Y-%m-%d")
                except Exception:
                    fecha_db = datetime.now().strftime("%Y-%m-%d")

                self.last_balance_date = fecha_db

                query = """
                SELECT 
                    TRIM(l.cuenta) AS cuenta,
                    COALESCE(MAX(NULLIF(TRIM(l.nombre_cuenta), '')), '-') AS nombre_cuenta,
                    SUM(COALESCE(CAST(l.debe AS REAL), 0)) AS debe,
                    SUM(COALESCE(CAST(l.haber AS REAL), 0)) AS haber,
                    COALESCE(arf.aprobado, arg.aprobado, 0) AS aprobado,
                    CASE WHEN COALESCE(arf.aprobado, arg.aprobado, 0) = 1 THEN 'X' ELSE '' END AS ok,
                    EXISTS(
                        SELECT 1 FROM ledger_runtime l2
                        WHERE TRIM(l2.cuenta) = TRIM(l.cuenta)
                          AND l2.fecha <= ?
                          AND COALESCE(TRIM(l2.ficha), '') NOT IN ('', '0', 'None', 'nan')
                          AND COALESCE(TRIM(l2.doc_referencia), '') NOT IN ('', '0 #0', 'nan #nan', '-')
                        GROUP BY l2.ficha, l2.doc_referencia
                        HAVING ABS(SUM(
                            COALESCE(CAST(l2.debe AS REAL), 0) - COALESCE(CAST(l2.haber AS REAL), 0)
                        )) > 0.1
                    ) AS tiene_auxiliar
                FROM ledger_runtime l
                LEFT JOIN account_reviews arf
                    ON TRIM(arf.cuenta) = TRIM(l.cuenta)
                   AND arf.fecha_balance = ?
                LEFT JOIN account_reviews arg
                    ON TRIM(arg.cuenta) = TRIM(l.cuenta)
                   AND arg.fecha_balance = 'GLOBAL'
                WHERE l.fecha <= ?
                GROUP BY TRIM(l.cuenta), COALESCE(arf.aprobado, arg.aprobado, 0)
                HAVING (? = 0 OR COALESCE(arf.aprobado, arg.aprobado, 0) = 0)
                ORDER BY TRIM(l.cuenta)
                """
                only_no = 1 if solo_no_aprobadas else 0
                df = run_query(query, db_name, params=[fecha_db, fecha_db, fecha_db, only_no])

                def classify_balance(row):
                    d, h = float(row["debe"]), float(row["haber"])
                    neto = d - h
                    digit = str(row["cuenta"])[0]
                    a = p = per = gan = 0
                    if digit in ["1", "2"]:
                        if neto >= 0:
                            a = neto
                        else:
                            p = abs(neto)
                    else:
                        if neto >= 0:
                            per = neto
                        else:
                            gan = abs(neto)
                    return pd.Series([a, p, per, gan])

                if not df.empty:
                    df[["activo", "pasivo", "perdida", "ganancia"]] = df.apply(classify_balance, axis=1)
                    ordered_cols = [c for c in df.columns if c not in ("aprobado", "ok")] + [
                        c for c in ("ok",) if c in df.columns
                    ]
                    df = df[ordered_cols]
                    total_a = df["activo"].sum()
                    total_p = df["pasivo"].sum()
                    total_per = df["perdida"].sum()
                    total_gan = df["ganancia"].sum()
                    diff_res = total_gan - total_per
                    label_diff = "UTILIDAD" if diff_res >= 0 else "PÉRDIDA"
                    summary = (
                        f"ACTIVO: {format_miles(total_a)} | PASIVO: {format_miles(total_p)} | "
                        f"{label_diff}: {format_miles(abs(diff_res))}"
                    )
                else:
                    summary = "Sin datos para la fecha seleccionada."

                export_call = lambda: export_balance_8_columnas(db_name, fecha_db)

            elif tipo == "pendientes_cuenta":
                fecha_corte = getattr(self, "last_balance_date", None) or datetime.now().strftime("%Y-%m-%d")
                cuenta = str(busca).strip()
                title = f"DOCUMENTOS PENDIENTES: {cuenta}"
                acc_name = cuenta
                acc_name_df = run_query(
                    "SELECT nombre_cuenta FROM ledger_runtime WHERE cuenta = ? LIMIT 1",
                    db_name,
                    params=[cuenta],
                )
                if acc_name_df is not None and not acc_name_df.empty:
                    acc_name = str(acc_name_df.iloc[0]["nombre_cuenta"] or cuenta).strip()
                    title = f"DOCUMENTOS PENDIENTES: {cuenta} - {acc_name}"

                q_pend = """
                SELECT
                    ficha AS [Auxiliar],
                    razon_social,
                    COALESCE(
                        NULLIF(TRIM(COALESCE(documento, '')), ''),
                        NULLIF(TRIM(COALESCE(doc_referencia, '')), ''),
                        '-'
                    ) AS [Documento Ref.],
                    SUM(COALESCE(CAST(debe AS REAL), 0) - COALESCE(CAST(haber AS REAL), 0)) AS [Saldo Pendiente]
                FROM ledger_runtime
                WHERE TRIM(cuenta) = TRIM(?) AND fecha <= ?
                GROUP BY ficha, razon_social, COALESCE(
                    NULLIF(TRIM(COALESCE(documento, '')), ''),
                    NULLIF(TRIM(COALESCE(doc_referencia, '')), ''),
                    '-'
                )
                HAVING ABS(SUM(COALESCE(CAST(debe AS REAL), 0) - COALESCE(CAST(haber AS REAL), 0))) > 0.1
                ORDER BY ficha, razon_social;
                """
                df = run_query(q_pend, db_name, params=[cuenta, fecha_corte])
                if df is not None and not df.empty:
                    summary = f"DEUDA PENDIENTE TOTAL: {format_miles(df['Saldo Pendiente'].sum())}"
                export_call = lambda: export_pendientes_cuenta(db_name, cuenta, acc_name, fecha_corte)

            elif tipo in {"informe_eess", "informe_pyg"}:
                payload = busca if isinstance(busca, dict) else {}
                y = int(payload.get("year") or datetime.now().year)
                m = int(payload.get("month") or datetime.now().month)
                start_iso, end_iso = _month_bounds(y, m)
                clasif = _load_clasificacion()
                if clasif is None or clasif.empty:
                    self.notify("No se encontró `clasificacion.xlsx` o está vacía.", severity="warning")
                    return

                if tipo == "informe_eess":
                    cf = clasif[clasif["Reporte"] == "EESS"].copy()
                    if cf.empty:
                        self.notify("No hay cuentas EESS en la clasificación.", severity="warning")
                        return
                    fe = _sql_ledger_fecha_eff("l")
                    q = f"""
                    WITH x AS (
                        SELECT l.*, {fe} AS fecha_eff
                        FROM ledger_runtime l
                    )
                    SELECT
                        TRIM(cuenta) AS cuenta,
                        COALESCE(MAX(NULLIF(TRIM(nombre_cuenta), '')), '-') AS nombre_cuenta,
                        SUM(COALESCE(CAST(debe AS REAL), 0) - COALESCE(CAST(haber AS REAL), 0)) AS saldo
                    FROM x
                    WHERE fecha_eff IS NOT NULL AND TRIM(CAST(fecha_eff AS TEXT)) <> ''
                      AND CAST(fecha_eff AS TEXT) <= ?
                    GROUP BY TRIM(cuenta)
                    """
                    bal = run_query(q, db_name, params=[end_iso])
                    if bal is None or bal.empty:
                        self.notify("No hay datos contables para ese período.", severity="warning")
                        return
                    work = cf.merge(bal, how="left", on="cuenta")
                    work["saldo"] = pd.to_numeric(work["saldo"], errors="coerce").fillna(0.0)
                    work = _prepare_informe_detail_df(work)
                    df_detail = work[
                        ["Detalle N1", "Detalle N2", "cuenta", "nombre_cuenta", "saldo"]
                    ].sort_values(["Detalle N1", "Detalle N2", "cuenta"])
                    pyg_ref = _compute_pyg_net_ytd(db_name, clasif, end_iso)
                    df = _build_eess_resumen_df(work, cf, pyg_ref)
                    title = f"EESS ACUMULADO A {m:02d}-{y}"
                    ta = float(df.attrs.get("ta", 0.0))
                    tp_disp = float(df.attrs.get("tp_disp", 0.0))
                    tpat = float(df.attrs.get("tpat", 0.0))
                    pas_mas_pat = tp_disp + tpat
                    cuadre = float(df.attrs.get("cuadre_diff", 0.0))
                    summary = (
                        f"TOTAL ACTIVOS: {format_miles(ta)} | TOTAL PASIVOS: {format_miles(tp_disp)} | "
                        f"PATRIMONIO: {format_miles(tpat)} | PAS.+PAT.: {format_miles(pas_mas_pat)} | "
                        f"PYG YTD (utilidad): {format_miles(pyg_ref)}"
                    )
                    if abs(cuadre) > 1.0:
                        summary += f" | Desvío cuadre: {format_miles(cuadre)}"
                    else:
                        summary += " | Cuadre OK"
                    export_call = lambda: export_informe_gestion_excel(
                        db_name,
                        f"informe_eess_{y}{m:02d}",
                        [
                            ("Resumen", df.drop(columns=["_style"], errors="ignore")),
                            ("Detalle_cuentas", df_detail),
                        ],
                    )
                    secondary = (
                        f"DETALLE CUENTAS EESS - {m:02d}-{y}",
                        df_detail,
                        lambda: export_dataframe_simple_excel(
                            db_name, df_detail, f"informe_eess_detalle_{y}{m:02d}", "detalle"
                        ),
                        f"Filas detalle: {len(df_detail)}",
                    )

                else:
                    title = f"PYG (Estado de Resultado) - {m:02d}-{y}"
                    cf = clasif[clasif["Reporte"] == "PYG"].copy()
                    if cf.empty:
                        self.notify("No hay cuentas PYG en la clasificación.", severity="warning")
                        return
                    fe = _sql_ledger_fecha_eff("l")
                    q = f"""
                    WITH x AS (
                        SELECT l.*, {fe} AS fecha_eff
                        FROM ledger_runtime l
                    )
                    SELECT
                        TRIM(cuenta) AS cuenta,
                        COALESCE(MAX(NULLIF(TRIM(nombre_cuenta), '')), '-') AS nombre_cuenta,
                        SUM(COALESCE(CAST(debe AS REAL), 0) - COALESCE(CAST(haber AS REAL), 0)) AS monto
                    FROM x
                    WHERE fecha_eff IS NOT NULL AND TRIM(CAST(fecha_eff AS TEXT)) <> ''
                      AND CAST(fecha_eff AS TEXT) >= ? AND CAST(fecha_eff AS TEXT) <= ?
                    GROUP BY TRIM(cuenta)
                    """
                    bal = run_query(q, db_name, params=[start_iso, end_iso])
                    if bal is None or bal.empty:
                        self.notify("No hay movimientos en ese mes.", severity="warning")
                        return
                    work = cf.merge(bal, how="left", on="cuenta")
                    work["monto"] = pd.to_numeric(work["monto"], errors="coerce").fillna(0.0)
                    work = _prepare_informe_detail_df(work)
                    df_detail = work[
                        ["Detalle N1", "Detalle N2", "cuenta", "nombre_cuenta", "monto"]
                    ].sort_values(["Detalle N1", "Detalle N2", "cuenta"])
                    df = _build_pyg_resumen_df(work, cf)
                    net = float(work["monto"].sum())
                    summary = (
                        f"RESULTADO NETO MES: {format_miles(net)} | F8: detalle por cuenta | "
                        f"Export: resumen + detalle"
                    )
                    export_call = lambda: export_informe_gestion_excel(
                        db_name,
                        f"informe_pyg_{y}{m:02d}",
                        [
                            ("Resumen", df),
                            ("Detalle_cuentas", df_detail),
                        ],
                    )
                    secondary = (
                        f"DETALLE CUENTAS PYG - {m:02d}-{y}",
                        df_detail,
                        lambda: export_dataframe_simple_excel(
                            db_name, df_detail, f"informe_pyg_detalle_{y}{m:02d}", "detalle"
                        ),
                        f"Filas detalle: {len(df_detail)}",
                    )

            elif tipo == "libro_ventas":
                payload = busca if isinstance(busca, dict) else {}
                y = int(payload.get("year") or datetime.now().year)
                m = int(payload.get("month") or datetime.now().month)
                periodo = f"{y:04d}{m:02d}"

                # Asegura que el libro esté materializado (si la base viene de antes).
                try:
                    refresh_libro_ventas_from_ledger(db_name, "clasificacion.xlsx")
                except Exception:
                    pass

                q = """
                SELECT
                    fecha,
                    tipo_doc,
                    comprobante,
                    folio,
                    rut_ficha,
                    razon_social,
                    unidad_de_negocio,
                    exento,
                    neto,
                    iva,
                    otros,
                    total
                FROM libro_ventas_docs
                WHERE periodo = ?
                ORDER BY fecha, folio, comprobante
                """
                df = run_query(q, db_name, params=[periodo])
                title = f"LIBRO DE VENTAS - {m:02d}-{y}"
                if df is None or df.empty:
                    self.notify("No hay documentos detectados en el libro de ventas para ese mes.", severity="warning")
                    return
                empresa_nombre, empresa_rut = session.get("empresa") or "", session.get("rut") or ""
                df_layout, df_res, summary = _build_libro_ventas_layout(df, y, m, empresa_nombre, empresa_rut)
                # Vista principal: layout estilo tabla Kame (con subtotales)
                export_call = lambda: export_dataframe_simple_excel(
                    db_name, df_layout, f"libro_ventas_{y}{m:02d}", sheet_name="libro_ventas"
                )
                export_res_call = lambda: export_dataframe_simple_excel(
                    db_name, df_res, f"libro_ventas_resumen_{y}{m:02d}", sheet_name="resumen"
                )
                secondary = (f"RESUMEN GENERAL LIBRO - {m:02d}-{y}", df_res, export_res_call, "")
                df = df_layout
            
            elif tipo == "comprobante":
                params = [f"%{busca}%"]
                query = f"SELECT {sql_fecha} as fecha, l.comprobante, l.cuenta, l.nombre_cuenta, l.debe, l.haber, l.concepto FROM ledger_runtime l WHERE l.comprobante LIKE ? ORDER BY l.comprobante, l.cuenta"
                df = run_query(query, db_name, params=params)
                if not df.empty:
                    summary = f"TOTAL ASIENTO: {format_miles(df['debe'].sum())}"
                export_call = lambda: export_report_to_excel(db_name, busca, tipo_analisis="comprobante")

            elif tipo == "notas_cuentas":
                title = "NOTAS DE CUENTAS"
                df = list_account_reviews(db_name)
                if not df.empty:
                    df["ok"] = df["aprobado"].apply(lambda x: "X" if int(x or 0) == 1 else "")
                    summary = f"NOTAS: {len(df)}"
                export_call = lambda: export_dataframe_simple_excel(
                    db_name, df, "notas_cuentas", sheet_name="notas"
                )

            elif tipo == "relacion_grupos":
                term = str(busca or "").strip() or "*"
                title = f"RELACIÓN CONCEPTOS/GRUPOS: {term}"
                pattern = term.upper().replace("*", "%")
                if "%" not in pattern:
                    pattern = f"%{pattern}%"
                query = f"""
                SELECT
                    {sql_fecha} as fecha,
                    l.comprobante,
                    l.cuenta,
                    l.nombre_cuenta,
                    l.ficha,
                    l.razon_social,
                    COALESCE(NULLIF(TRIM(COALESCE(l.documento, '')), ''), NULLIF(TRIM(COALESCE(l.doc_referencia, '')), ''), '-') AS doc_referencia,
                    CAST(COALESCE(l.debe, 0) AS REAL) AS debe,
                    CAST(COALESCE(l.haber, 0) AS REAL) AS haber,
                    l.concepto
                FROM ledger_runtime l
                WHERE UPPER(COALESCE(l.cuenta, '')) LIKE ?
                   OR UPPER(COALESCE(l.nombre_cuenta, '')) LIKE ?
                   OR UPPER(COALESCE(l.concepto, '')) LIKE ?
                ORDER BY l.cuenta, fecha, l.comprobante
                """
                df = run_query(query, db_name, params=[pattern, pattern, pattern])
                if not df.empty:
                    fecha_raw = df["fecha"].astype(str)
                    fecha_dt = pd.to_datetime(df["fecha"], errors="coerce")
                    df["fecha"] = fecha_dt.dt.strftime("%d-%m-%Y")
                    df.loc[fecha_dt.isna(), "fecha"] = fecha_raw[fecha_dt.isna()]
                    df["mes"] = fecha_dt.dt.month.apply(lambda m: f"{int(m):02d}" if pd.notna(m) else "")
                    df["saldo"] = pd.to_numeric(df["debe"], errors="coerce").fillna(0) - pd.to_numeric(df["haber"], errors="coerce").fillna(0)
                    ordered = [
                        "fecha", "mes", "comprobante", "cuenta", "nombre_cuenta",
                        "ficha", "razon_social", "doc_referencia", "debe", "haber", "saldo", "concepto",
                    ]
                    df = df[[c for c in ordered if c in df.columns]]
                    summary = (
                        f"REGISTROS: {len(df)} | "
                        f"DEBE: {format_miles(df['debe'].sum())} | "
                        f"HABER: {format_miles(df['haber'].sum())} | "
                        f"SALDO: {format_miles(df['saldo'].sum())}"
                    )
                safe_term = re.sub(r"[^A-Za-z0-9_-]+", "_", term)[:40] or "todos"
                export_call = lambda: export_dataframe_simple_excel(
                    db_name, df, f"relacion_grupos_{safe_term}", sheet_name="relacion_grupos"
                )

            else:
                terminos = [t.strip() for t in busca.split(",") if t.strip()]
                if not terminos:
                    terminos = ["*"]
                params = []
                filtros = []
                for t in terminos:
                    pattern = t.upper().replace("*", "%")
                    if "%" not in pattern:
                        pattern = f"%{pattern}%"
                    params.extend([pattern, pattern, pattern, pattern])
                    filtros.append("(UPPER(l.nombre_cuenta) LIKE ? OR UPPER(l.razon_social) LIKE ? OR UPPER(l.ficha) LIKE ? OR UPPER(l.concepto) LIKE ?)")
                
                where_busqueda = " OR ".join(filtros)
                
                if tipo == "inteligente":
                    query = f"""
                    SELECT
                        COALESCE(NULLIF(TRIM(l.ficha), ''), '-') AS [Auxiliar],
                        COALESCE(NULLIF(TRIM(l.razon_social), ''), '-') AS razon_social,
                        COALESCE(NULLIF(TRIM(COALESCE(l.documento, '')), ''), NULLIF(TRIM(COALESCE(l.doc_referencia, '')), ''), '-') AS [Documento Ref.],
                        SUM(COALESCE(CAST(l.debe AS REAL), 0) - COALESCE(CAST(l.haber AS REAL), 0)) AS [Saldo Pendiente]
                    FROM ledger_runtime l
                    WHERE ({where_busqueda})
                    GROUP BY
                        COALESCE(NULLIF(TRIM(l.ficha), ''), '-'),
                        COALESCE(NULLIF(TRIM(l.razon_social), ''), '-'),
                        COALESCE(NULLIF(TRIM(COALESCE(l.documento, '')), ''), NULLIF(TRIM(COALESCE(l.doc_referencia, '')), ''), '-')
                    HAVING ABS(SUM(COALESCE(CAST(l.debe AS REAL), 0) - COALESCE(CAST(l.haber AS REAL), 0))) > 0.1
                    ORDER BY [Auxiliar], razon_social;
                    """
                    title = f"ANÁLISIS INTELIGENTE (PENDIENTES): {busca}"
                else:
                    query = f"""
                    WITH saldos_doc AS (
                        SELECT cuenta, ficha, COALESCE(documento, doc_referencia) AS doc_key,
                               SUM(COALESCE(CAST(debe AS REAL), 0) - COALESCE(CAST(haber AS REAL), 0)) as saldo_neto
                        FROM ledger_runtime
                        GROUP BY cuenta, ficha, COALESCE(documento, doc_referencia)
                    )
                    SELECT
                        {sql_fecha} as fecha, l.comprobante, l.cuenta, l.nombre_cuenta, l.ficha, l.razon_social,
                        COALESCE(NULLIF(TRIM(COALESCE(l.documento, '')), ''), NULLIF(TRIM(COALESCE(l.doc_referencia, '')), ''), '-') AS documento,
                        l.vencimiento,
                        (COALESCE(CAST(l.debe AS REAL), 0) - COALESCE(CAST(l.haber AS REAL), 0)) AS saldo,
                        CASE WHEN ABS(s.saldo_neto) > 0.1 THEN 'si' ELSE 'no' END as pendiente,
                        l.debe, l.haber, l.concepto
                    FROM ledger_runtime l
                    JOIN saldos_doc s
                      ON l.cuenta = s.cuenta
                     AND COALESCE(l.ficha, '') = COALESCE(s.ficha, '')
                     AND COALESCE(COALESCE(l.documento, l.doc_referencia), '') = COALESCE(s.doc_key, '')
                    WHERE ({where_busqueda})
                    ORDER BY l.cuenta, l.fecha
                    """
                    title = f"HISTÓRICO COMPLETO: {busca}"
                
                df = run_query(query, db_name, params=params)
                if not df.empty:
                    if 'Saldo Pendiente' in df.columns:
                        summary = f"DEUDA PENDIENTE TOTAL: {format_miles(df['Saldo Pendiente'].sum())}"
                    else:
                        summary = f"REGISTROS ENCONTRADOS: {len(df)}"
                export_call = lambda: export_report_to_excel(db_name, busca, tipo_analisis=tipo)

            if df is not None and not df.empty:
                secondary_payload = locals().get("secondary", None)
                self.push_screen(ReportPreview(title, df, export_call, summary, secondary=secondary_payload))
            else:
                self.notify(f"No se encontró información para '{busca}'", severity="error")
        except Exception as e:
            self.notify(f"Error: {str(e)}", severity="error")

    def get_metadata(self, db_name):
        try:
            meta = run_query("SELECT empresa_nombre, empresa_rut FROM metadata LIMIT 1", db_name)
            if meta is not None and not meta.empty:
                return meta.iloc[0]['empresa_nombre'], meta.iloc[0]['empresa_rut']
        except: pass
        return None, None

if __name__ == "__main__":
    app = KamePremium()
    app.run()
