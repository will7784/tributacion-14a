import os
import json
import re
from io import BytesIO
from pathlib import Path
from datetime import datetime

from flask import Flask, render_template, request, jsonify, send_file, session, redirect, url_for
import pandas as pd

from core.db import (
    init_db, get_empresa, guardar_empresa, get_plan_cuentas, guardar_plan_cuentas,
    get_ledger, limpiar_ledger, guardar_ledger,
    get_comprobantes, get_comprobante_lineas, guardar_comprobante,
    get_ajustes_tributarios, guardar_ajustes_tributarios,
    get_clasificacion, guardar_clasificacion, DB_DIR,
    get_dj1847_overrides, guardar_dj1847_overrides,
    get_dj1847_periodo, guardar_dj1847_periodo,
    get_folios_usados, guardar_folio_usado,
)
from core.balance import get_balance_data
from core.plan_cuentas import homologar_plan, cargar_plan_sii, cargar_plan_base, buscar_cuentas_base, cargar_codigos_f22
from core.auth import init_auth_db, create_user, verify_user, get_user, login_required
from parsers.csv_import import parse_csv
from parsers.excel_import import parse_excel

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "tax14a-secret-key-change-in-production")

# Inicializar DB de auth al importar el módulo (Gunicorn no ejecuta __main__)
init_auth_db()


def _ensure_admin():
    """Crea el usuario admin desde variables de entorno si no existe ninguno."""
    from core.auth import _get_conn, create_user
    try:
        conn = _get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        count = cursor.fetchone()[0]
        conn.close()
        if count == 0:
            admin_user = os.environ.get("ADMIN_USERNAME", "will")
            admin_pass = os.environ.get("ADMIN_PASSWORD")
            if admin_pass:
                create_user(admin_user, admin_pass, is_superuser=True)
                print(f"[INIT] Admin user '{admin_user}' created")
    except Exception:
        pass


_ensure_admin()


# ============================================================
# HELPERS
# ============================================================
def _clean_rut(rut: str) -> str:
    return rut.replace(".", "").replace("-", "").upper()


def _get_db_path(rut: str) -> Path:
    return Path(f"{_clean_rut(rut)}_14a.db")


# ============================================================
# AUTH
# ============================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.is_json:
            data = request.get_json(silent=True) or {}
        else:
            data = request.form
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", "")).strip()
        if verify_user(username, password):
            session["user"] = username
            if request.is_json:
                return jsonify({"ok": True})
            return redirect(url_for("index"))
        if request.is_json:
            return jsonify({"error": "Credenciales inválidas"}), 401
        return render_template("login.html", error="Credenciales inválidas")
    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))


# ============================================================
# PAGES
# ============================================================
@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/importar")
@login_required
def importar():
    return render_template("importar.html")


@app.route("/homologacion")
@login_required
def homologacion():
    return render_template("homologacion.html")


@app.route("/plan_base")
@login_required
def plan_base_page():
    return render_template("plan_base.html")


@app.route("/clasificacion")
@login_required
def clasificacion():
    return render_template("clasificacion.html")


@app.route("/comprobantes")
@login_required
def comprobantes():
    return render_template("comprobantes.html")


@app.route("/balance")
@login_required
def balance():
    return render_template("balance.html")


@app.route("/ajustes")
@login_required
def ajustes():
    return render_template("ajustes.html")


@app.route("/dj1847")
@login_required
def dj1847():
    return render_template("dj1847.html")


@app.route("/dj1847/print/<rut>")
@login_required
def dj1847_print(rut):
    clean = _clean_rut(rut)
    fecha = request.args.get("fecha", datetime.now().strftime("%Y-%m-%d"))
    periodo = request.args.get("periodo", datetime.now().strftime("%Y"))
    emp = get_empresa(clean) or {}
    data = _build_dj1847_data(clean, fecha)
    return render_template(
        "dj1847_print.html",
        rut=clean,
        nombre=emp.get("nombre", ""),
        periodo=periodo,
        fecha=datetime.now().strftime("%d-%m-%Y"),
        filas=data.get("filas", []),
    )


@app.route("/dj1926")
@login_required
def dj1926():
    return render_template("dj1926.html")


# ============================================================
# API - EMPRESA
# ============================================================
@app.route("/api/empresas", methods=["GET"])
@login_required
def api_empresas():
    db_files = sorted([f for f in os.listdir(DB_DIR) if f.endswith("_14a.db")])
    empresas = []
    for db in db_files:
        rut_raw = db.replace("_14a.db", "")
        emp = get_empresa(rut_raw)
        if emp:
            empresas.append({"rut": rut_raw, "nombre": emp.get("nombre", rut_raw)})
        else:
            empresas.append({"rut": rut_raw, "nombre": rut_raw})
    return jsonify(empresas)


@app.route("/api/empresas", methods=["POST"])
@login_required
def api_empresas_post():
    data = request.get_json() or {}
    rut = str(data.get("rut", "")).strip()
    nombre = str(data.get("nombre", "")).strip()
    if not rut or not nombre:
        return jsonify({"error": "RUT y nombre son obligatorios"}), 400
    clean = _clean_rut(rut)
    guardar_empresa(
        clean,
        nombre,
        actividad_principal=data.get("actividad_principal", ""),
        entidad_supervisora=data.get("entidad_supervisora", "NO APLICA"),
        anio_ifrs=data.get("anio_ifrs", 0),
        rep_legal_nombre=data.get("rep_legal_nombre", ""),
        rep_legal_rut=data.get("rep_legal_rut", ""),
    )
    return jsonify({"rut": clean, "nombre": nombre})


@app.route("/api/empresa/<rut>", methods=["GET"])
@login_required
def api_empresa_get(rut):
    clean = _clean_rut(rut)
    emp = get_empresa(clean)
    if not emp:
        return jsonify({"error": "No encontrada"}), 404
    return jsonify(emp)


@app.route("/api/empresa/<rut>", methods=["POST"])
@login_required
def api_empresa_post(rut):
    clean = _clean_rut(rut)
    data = request.get_json() or {}
    nombre = str(data.get("nombre", "")).strip()
    if not nombre:
        return jsonify({"error": "Nombre es obligatorio"}), 400
    guardar_empresa(
        clean,
        nombre,
        actividad_principal=data.get("actividad_principal", ""),
        entidad_supervisora=data.get("entidad_supervisora", "NO APLICA"),
        anio_ifrs=data.get("anio_ifrs", 0),
        rep_legal_nombre=data.get("rep_legal_nombre", ""),
        rep_legal_rut=data.get("rep_legal_rut", ""),
    )
    return jsonify({"ok": True})


# ============================================================
# API - IMPORTAR
# ============================================================
@app.route("/api/importar/<rut>", methods=["POST"])
@login_required
def api_importar(rut):
    clean = _clean_rut(rut)
    if "file" not in request.files:
        return jsonify({"error": "No se envió archivo"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "Nombre vacío"}), 400

    ext = file.filename.split(".")[-1].lower()
    bytes_data = file.read()

    try:
        if ext == "csv":
            df_raw = parse_csv(bytes_data)
        else:
            df_raw = parse_excel(bytes_data)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    # Detección robusta de columnas
    def _norm(name):
        return name.lower().strip().replace("$", "").replace(".", "").replace("  ", " ")

    col_map = {}
    for c in df_raw.columns:
        cl = _norm(c)
        cl_ns = cl.replace(" ", "")
        if cl in ("cuenta", "codigo", "code", "id cuenta") or cl_ns in ("cuenta", "codigo", "code", "idcuenta"):
            col_map["cuenta"] = c
        elif cl in ("nombre", "descripcion", "glosa", "nombre cuenta", "nombre de cuenta") or cl_ns in ("nombre", "descripcion", "glosa", "nombrecuenta"):
            col_map["nombre"] = c
        elif cl in ("debe", "debito", "cargo", "debe ") or cl_ns in ("debe", "debito", "cargo"):
            col_map["debe"] = c
        elif cl in ("haber", "credito", "abono", "haber ") or cl_ns in ("haber", "credito", "abono"):
            col_map["haber"] = c
        elif cl in ("fecha", "date") or cl_ns in ("fecha", "date"):
            col_map["fecha"] = c
        elif cl in ("comprobante", "folio", "asiento", "número", "nro comprobante") or cl_ns in ("comprobante", "folio", "asiento", "numero", "nrocomprobante"):
            col_map["comprobante"] = c
        elif cl in ("tipo comprobante", "tipocomprobante", "tipo", "tipo de comprobante") or cl_ns in ("tipocomprobante", "tipodecomprobante"):
            col_map["tipo_comprobante"] = c
        elif cl in ("rut ficha", "rut", "rutficha", "rut cliente") or cl_ns in ("rutficha", "rutcliente"):
            col_map["rut_ficha"] = c
        elif cl in ("rzn social ficha", "razon social", "nombre ficha", "razón social", "nombre cliente") or cl_ns in ("rznsocialficha", "razonsocial", "nombreficha", "nombrecliente"):
            col_map["razon_social"] = c
        elif cl in ("comentario linea", "glosa linea", "concepto", "glosa") or cl_ns in ("comentariolinea", "glosalinea", "concepto", "glosa"):
            col_map["concepto"] = c
        elif cl in ("documento", "folio doc", "nro doc", "número documento") or cl_ns in ("documento", "foliodoc", "nrodoc", "numerodocumento"):
            col_map["documento"] = c
        elif cl in ("fecha venc", "vencimiento", "fecha de vencimiento") or cl_ns in ("fechavenc", "vencimiento", "fechadevencimiento"):
            col_map["fecha_venc"] = c
        elif cl in ("unidad negocio", "unidad_de_negocio", "proyecto", "unidad de negocio") or cl_ns in ("unidadnegocio", "unidaddenegocio"):
            col_map["unidad_negocio"] = c
        elif cl in ("tipo movimiento", "tipomovimiento", "tipo de movimiento") or cl_ns in ("tipomovimiento", "tipodemovimiento"):
            col_map["tipo_movimiento"] = c
        elif cl in ("número movimiento", "numeromovimiento", "nro mov", "número de movimiento") or cl_ns in ("numeromovimiento", "nromov", "numerodemovimiento"):
            col_map["numero_movimiento"] = c

    if "cuenta" not in col_map or "debe" not in col_map or "haber" not in col_map:
        return jsonify({"error": f"No se detectaron columnas obligatorias. Encontradas: {list(col_map.keys())}"}), 400

    cuenta_raw = df_raw[col_map["cuenta"]].astype(str).str.strip()
    df = pd.DataFrame()
    df["cuenta"] = cuenta_raw.str.extract(r"^\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", expand=False).fillna(cuenta_raw)
    nombre_col = col_map.get("nombre")
    cuenta_col_detected = col_map.get("cuenta")
    if nombre_col and nombre_col != cuenta_col_detected:
        df["nombre_cuenta"] = df_raw[nombre_col].astype(str).str.strip()
    else:
        df["nombre_cuenta"] = cuenta_raw.str.replace(r"^\s*[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\s*", "", regex=True).str.strip()
        df.loc[df["nombre_cuenta"] == "", "nombre_cuenta"] = df["cuenta"]

    df["debe"] = pd.to_numeric(df_raw[col_map["debe"]], errors="coerce").fillna(0)
    df["haber"] = pd.to_numeric(df_raw[col_map["haber"]], errors="coerce").fillna(0)
    df["fecha"] = pd.to_datetime(df_raw[col_map.get("fecha", "fecha")], errors="coerce", dayfirst=True).dt.strftime("%Y-%m-%d") if "fecha" in col_map else ""
    df["comprobante"] = df_raw[col_map.get("comprobante", "comprobante")].astype(str) if "comprobante" in col_map else ""
    df["tipo_comprobante"] = df_raw[col_map.get("tipo_comprobante", "tipo_comprobante")].astype(str) if "tipo_comprobante" in col_map else ""
    df["concepto"] = df_raw[col_map.get("concepto", "concepto")].astype(str) if "concepto" in col_map else ""
    df["rut_ficha"] = df_raw[col_map.get("rut_ficha", "rut_ficha")].astype(str) if "rut_ficha" in col_map else ""
    df["razon_social"] = df_raw[col_map.get("razon_social", "razon_social")].astype(str) if "razon_social" in col_map else ""
    df["documento"] = df_raw[col_map.get("documento", "documento")].astype(str) if "documento" in col_map else ""
    df["fecha_venc"] = pd.to_datetime(df_raw[col_map.get("fecha_venc", "fecha_venc")], errors="coerce", dayfirst=True).dt.strftime("%Y-%m-%d") if "fecha_venc" in col_map else ""
    df["unidad_negocio"] = df_raw[col_map.get("unidad_negocio", "unidad_negocio")].astype(str) if "unidad_negocio" in col_map else ""
    df["tipo_movimiento"] = df_raw[col_map.get("tipo_movimiento", "tipo_movimiento")].astype(str) if "tipo_movimiento" in col_map else ""
    df["numero_movimiento"] = df_raw[col_map.get("numero_movimiento", "numero_movimiento")].astype(str) if "numero_movimiento" in col_map else ""
    df["origen"] = "IMPORTADO"

    limpiar_ledger(clean)
    guardar_ledger(df, clean)

    # Homologar plan de cuentas
    cuentas_import = df.groupby("cuenta")["nombre_cuenta"].first().reset_index().rename(columns={"nombre_cuenta": "nombre"})
    hom = homologar_plan(cuentas_import)
    guardar_plan_cuentas(clean, hom)

    return jsonify({"ok": True, "filas": len(df), "cuentas": len(hom)})


# ============================================================
# API - PLAN BASE (global)
# ============================================================
@app.route("/api/plan_base", methods=["GET"])
@login_required
def api_plan_base_get():
    df = cargar_plan_base()
    return jsonify(df.fillna("").to_dict(orient="records"))


@app.route("/api/plan_base", methods=["POST"])
@login_required
def api_plan_base_post():
    data = request.get_json() or []
    if not data:
        return jsonify({"error": "Datos vacíos"}), 400
    df = pd.DataFrame(data)
    from core.plan_cuentas import guardar_plan_base
    guardar_plan_base(df)
    return jsonify({"ok": True})


@app.route("/api/buscar_plan_base", methods=["GET"])
@login_required
def api_buscar_plan_base():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])
    resultados = buscar_cuentas_base(query)
    return jsonify(resultados)


# ============================================================
# API - HOMOLOGACIÓN (por empresa)
# ============================================================
@app.route("/api/plan_cuentas/<rut>", methods=["GET"])
@login_required
def api_plan_cuentas_get(rut):
    clean = _clean_rut(rut)
    df = get_plan_cuentas(clean)
    if df.empty:
        return jsonify([])
    # Enriquecer con nombre base
    base = cargar_plan_base()
    if not base.empty:
        base_map = base.set_index("Cuenta")["Nombre"].to_dict()
        df["nombre_base"] = df["cuenta_base"].map(base_map).fillna("")
    return jsonify(df.fillna("").to_dict(orient="records"))


@app.route("/api/plan_cuentas/<rut>", methods=["POST"])
@login_required
def api_plan_cuentas_post(rut):
    clean = _clean_rut(rut)
    data = request.get_json() or []
    if not data:
        return jsonify({"error": "Datos vacíos"}), 400
    df = pd.DataFrame(data)
    # Solo permitir columnas de homologación
    cols_ok = ["cuenta_local", "nombre_local", "cuenta_base"]
    df = df[[c for c in cols_ok if c in df.columns]]
    guardar_plan_cuentas(clean, df)
    return jsonify({"ok": True})


@app.route("/api/plan_sii", methods=["GET"])
@login_required
def api_plan_sii():
    df = cargar_plan_sii()
    return jsonify(df.fillna("").to_dict(orient="records"))


@app.route("/api/codigos_f22", methods=["GET"])
@login_required
def api_codigos_f22():
    df = cargar_codigos_f22()
    return jsonify(df.fillna("").to_dict(orient="records"))


@app.route("/api/debug/plan", methods=["GET"])
@login_required
def api_debug_plan():
    import os
    from core.plan_cuentas import PLAN_BASE_PATH, _repo_plan_base_path, DATA_DIR
    repo = _repo_plan_base_path()
    base = cargar_plan_base()
    return jsonify({
        "data_dir": str(DATA_DIR),
        "data_dir_exists": DATA_DIR.exists(),
        "plan_base_path": str(PLAN_BASE_PATH),
        "plan_base_exists": PLAN_BASE_PATH.exists(),
        "plan_base_size": PLAN_BASE_PATH.stat().st_size if PLAN_BASE_PATH.exists() else 0,
        "repo_path": str(repo),
        "repo_exists": repo.exists(),
        "repo_size": repo.stat().st_size if repo.exists() else 0,
        "plan_rows": len(base),
        "plan_columns": list(base.columns),
        "sample": base.head(5).fillna("").to_dict(orient="records"),
    })


# ============================================================
# API - CLASIFICACIÓN (por empresa)
# ============================================================
@app.route("/api/clasificacion/<rut>", methods=["GET"])
@login_required
def api_clasificacion_get(rut):
    clean = _clean_rut(rut)
    df = get_clasificacion(clean)
    if df.empty:
        return jsonify([])
    return jsonify(df.fillna("").to_dict(orient="records"))


@app.route("/api/clasificacion/<rut>", methods=["POST"])
@login_required
def api_clasificacion_post(rut):
    clean = _clean_rut(rut)
    data = request.get_json() or []
    if not data:
        return jsonify({"error": "Datos vacíos"}), 400
    df = pd.DataFrame(data)
    guardar_clasificacion(clean, df)
    return jsonify({"ok": True})


# ============================================================
# API - COMPROBANTES
# ============================================================
@app.route("/api/comprobantes/<rut>", methods=["GET"])
@login_required
def api_comprobantes_get(rut):
    clean = _clean_rut(rut)
    comps = get_comprobantes(clean)
    if comps.empty:
        return jsonify([])
    result = []
    for _, comp in comps.iterrows():
        lineas = get_comprobante_lineas(clean, comp["id"])
        item = comp.to_dict()
        item["lineas"] = lineas.fillna("").to_dict(orient="records") if not lineas.empty else []
        result.append(item)
    return jsonify(result)


@app.route("/api/comprobantes/<rut>", methods=["POST"])
@login_required
def api_comprobantes_post(rut):
    clean = _clean_rut(rut)
    data = request.get_json() or {}
    fecha = data.get("fecha", "")
    glosa = data.get("glosa", "")
    tipo_norma = data.get("tipo_norma", "CYT")
    numero = data.get("numero", "")
    lineas = data.get("lineas", [])
    if not lineas:
        return jsonify({"error": "Sin líneas"}), 400
    total_debe = sum(float(l.get("debe", 0)) for l in lineas)
    total_haber = sum(float(l.get("haber", 0)) for l in lineas)
    if abs(total_debe - total_haber) > 0.01:
        return jsonify({"error": f"No cuadra: Debe {total_debe:,.0f} != Haber {total_haber:,.0f}"}), 400
    comp_id = guardar_comprobante(clean, fecha, glosa, tipo_norma, numero, lineas)
    return jsonify({"ok": True, "id": comp_id})


# ============================================================
# API - BALANCE
# ============================================================
@app.route("/api/balance/<rut>", methods=["GET"])
@login_required
def api_balance(rut):
    clean = _clean_rut(rut)
    fecha = request.args.get("fecha", datetime.now().strftime("%Y-%m-%d"))
    norma = request.args.get("norma", "CONTABLE")
    df = get_balance_data(clean, fecha, norma)
    if df.empty:
        return jsonify({"filas": [], "totales": {}})
    records = df.fillna("").to_dict(orient="records")
    # Totales
    totales = {
        "debe": float(df["debe"].sum()),
        "haber": float(df["haber"].sum()),
        "activo": float(df["activo"].sum()),
        "pasivo": float(df["pasivo"].sum()),
        "perdida": float(df["perdida"].sum()),
        "ganancia": float(df["ganancia"].sum()),
    }
    return jsonify({"filas": records, "totales": totales})


@app.route("/api/exportar_balance/<rut>", methods=["GET"])
@login_required
def api_exportar_balance(rut):
    clean = _clean_rut(rut)
    fecha = request.args.get("fecha", datetime.now().strftime("%Y-%m-%d"))
    norma = request.args.get("norma", "CONTABLE")
    df = get_balance_data(clean, fecha, norma)
    output = f"balance_{norma}_{fecha}.xlsx"
    df.drop(columns=["_es_total"], errors="ignore").to_excel(output, index=False)
    return send_file(output, as_attachment=True)


# ============================================================
# API - AJUSTES
# ============================================================
@app.route("/api/ajustes/<rut>", methods=["GET"])
@login_required
def api_ajustes_get(rut):
    clean = _clean_rut(rut)
    df = get_ajustes_tributarios(clean)
    if df.empty:
        return jsonify([])
    return jsonify(df.fillna("").to_dict(orient="records"))


@app.route("/api/ajustes/<rut>", methods=["POST"])
@login_required
def api_ajustes_post(rut):
    clean = _clean_rut(rut)
    data = request.get_json() or []
    if not data:
        return jsonify({"error": "Datos vacíos"}), 400
    df = pd.DataFrame(data)
    guardar_ajustes_tributarios(clean, df)
    return jsonify({"ok": True})


# ============================================================
# API - DJ1847
# ============================================================
def _padre_nivel3(cuenta: str) -> str:
    partes = cuenta.split('.')
    if len(partes) == 4:
        partes[3] = '00'
        return '.'.join(partes)
    return cuenta


# Orden oficial de columnas según instructivo SII DJ1847, Sección C
DJ1847_COLUMNAS = [
    "n",
    "cuenta",
    "cuenta_sii",
    "nombre",
    "debe",
    "haber",
    "saldo_deudor",
    "saldo_acreedor",
    "activo",
    "pasivo",
    "perdida",
    "ganancia",
    "cod_f22",
    "valor_tributario",
]

DJ1847_HEADERS = {
    "n": "N°",
    "cuenta": "Id. plan de cuentas utilizado en registros contables",
    "cuenta_sii": "Id. cuenta según clasificador de cuentas",
    "nombre": "Nombre de la Cuenta según registros contables",
    "debe": "Débitos",
    "haber": "Créditos",
    "saldo_deudor": "Saldo Deudor",
    "saldo_acreedor": "Saldo Acreedor",
    "activo": "Activo",
    "pasivo": "Pasivo",
    "perdida": "Pérdidas",
    "ganancia": "Ganancias",
    "cod_f22": "Conceptos y/o Partidas Que Componen el Resultado Financiero",
    "valor_tributario": "Valor Tributario",
}


def _build_dj1847_data(clean_rut, fecha=None):
    """Lógica pura de construcción de datos DJ1847, sin dependencia de Flask request."""
    if fecha is None:
        fecha = datetime.now().strftime("%Y-%m-%d")
    balance = get_balance_data(clean_rut, fecha, "TRIBUTARIO")
    plan = get_plan_cuentas(clean_rut)
    base = cargar_plan_base()
    if balance.empty:
        return {"filas": [], "totales": {}}
    bal = balance[~balance["_es_total"]].copy()
    # Solo cuentas nivel 4 (detalle)
    bal = bal[~bal["cuenta"].str.endswith(".00")].copy()
    # Merge con homologación para obtener cuenta_base (si existe)
    if not plan.empty:
        hom_map = plan.set_index("cuenta_local")["cuenta_base"].to_dict()
        bal["cuenta_base"] = bal["cuenta"].map(hom_map).fillna("")
    else:
        bal["cuenta_base"] = ""
    # Padre nivel 3 para buscar SII y F22
    def _inferir_padre_n3(row):
        if row["cuenta_base"]:
            return _padre_nivel3(row["cuenta_base"])
        partes = str(row["cuenta"]).strip().split('.')
        if len(partes) == 4:
            partes[3] = '00'
            return '.'.join(partes)
        return row["cuenta"]
    bal["padre_n3"] = bal.apply(_inferir_padre_n3, axis=1)
    # Merge con plan base para obtener cuenta_sii y cod_f22 del padre
    if not base.empty:
        base_sii_map = base.set_index("Cuenta")["cuenta_sii"].to_dict()
        bal["cuenta_sii"] = bal["padre_n3"].map(base_sii_map).fillna("")
        base_f22_map = base.set_index("Cuenta")["cod_f22"].to_dict()
        bal["cod_f22"] = bal["padre_n3"].map(base_f22_map).fillna("")
    else:
        bal["cuenta_sii"] = ""
        bal["cod_f22"] = ""
    # Valor Tributario: solo Activo/Pasivo; pasivos con signo negativo según instructivo SII
    def _calc_valor_tributario(row):
        cuenta = str(row["cuenta"])
        if cuenta.startswith(("1", "2")):
            activo = float(row.get("activo", 0) or 0)
            pasivo = float(row.get("pasivo", 0) or 0)
            if pasivo > 0:
                return -pasivo
            return activo
        return 0
    bal["valor_tributario"] = bal.apply(_calc_valor_tributario, axis=1)
    # Aplicar overrides guardados (cod_f22 y valor_tributario_manual)
    overrides = get_dj1847_overrides(clean_rut)
    if not overrides.empty:
        overrides = overrides.set_index("cuenta")
        # Override cod_f22
        if "cod_f22" in overrides.columns:
            bal["cod_f22"] = bal["cuenta"].map(overrides["cod_f22"]).fillna(bal["cod_f22"])
        # Override valor_tributario_manual (solo si no es null)
        if "valor_tributario_manual" in overrides.columns:
            vt_manual = overrides["valor_tributario_manual"].dropna()
            bal["valor_tributario"] = bal["cuenta"].map(vt_manual).fillna(bal["valor_tributario"])
    records = []
    for i, (_, r) in enumerate(bal.iterrows(), start=1):
        records.append({
            "n": i,
            "cuenta": r["cuenta"],
            "cuenta_sii": r["cuenta_sii"],
            "nombre": r["nombre_cuenta"],
            "debe": float(r["debe"]),
            "haber": float(r["haber"]),
            "saldo_deudor": float(r["saldo_deudor"]),
            "saldo_acreedor": float(r["saldo_acreedor"]),
            "activo": float(r["activo"]),
            "pasivo": float(r["pasivo"]),
            "perdida": float(r["perdida"]),
            "ganancia": float(r["ganancia"]),
            "cod_f22": r["cod_f22"] if pd.notna(r["cod_f22"]) else "",
            "valor_tributario": float(r["valor_tributario"]),
        })
    return {"filas": records, "totales": {}}


@app.route("/api/dj1847/<rut>", methods=["GET"])
@login_required
def api_dj1847(rut):
    clean = _clean_rut(rut)
    fecha = request.args.get("fecha", datetime.now().strftime("%Y-%m-%d"))
    return jsonify(_build_dj1847_data(clean, fecha))


@app.route("/api/dj1847_overrides/<rut>", methods=["GET"])
@login_required
def api_dj1847_overrides_get(rut):
    clean = _clean_rut(rut)
    df = get_dj1847_overrides(clean)
    if df.empty:
        return jsonify([])
    return jsonify(df.fillna("").to_dict(orient="records"))


@app.route("/api/dj1847_overrides/<rut>", methods=["POST"])
@login_required
def api_dj1847_overrides_post(rut):
    clean = _clean_rut(rut)
    data = request.get_json() or []
    if not data:
        return jsonify({"error": "Datos vacíos"}), 400
    df = pd.DataFrame(data)
    # Asegurar columnas correctas
    for col in ["cuenta", "cod_f22", "valor_tributario_manual"]:
        if col not in df.columns:
            df[col] = None
    df = df[["cuenta", "cod_f22", "valor_tributario_manual"]]
    # Convertir valor_tributario_manual a numérico, dejar vacío como NaN
    df["valor_tributario_manual"] = pd.to_numeric(df["valor_tributario_manual"], errors="coerce")
    # Filtrar filas vacías (sin cod_f22 ni valor_tributario_manual)
    df = df[(df["cod_f22"].notna() & (df["cod_f22"] != "")) | df["valor_tributario_manual"].notna()]
    guardar_dj1847_overrides(clean, df)
    return jsonify({"ok": True})


@app.route("/api/dj1847_periodo/<rut>", methods=["GET"])
@login_required
def api_dj1847_periodo_get(rut):
    clean = _clean_rut(rut)
    periodo = request.args.get("periodo", datetime.now().strftime("%Y"))
    data = get_dj1847_periodo(clean, periodo)
    if not data:
        # Pre-llenar con datos de la empresa
        emp = get_empresa(clean) or {}
        data = {
            "rut": clean,
            "periodo": periodo,
            "actividad_economica": emp.get("actividad_principal", ""),
            "entidad_supervisora": emp.get("entidad_supervisora", "NO APLICA"),
            "anio_ifrs": emp.get("anio_ifrs", 0) or 0,
            "folio_ini": None,
            "folio_fin": None,
            "ajustes_rli": 2,
        }
    return jsonify(data)


@app.route("/api/dj1847_periodo/<rut>", methods=["POST"])
@login_required
def api_dj1847_periodo_post(rut):
    clean = _clean_rut(rut)
    data = request.get_json() or {}
    periodo = str(data.get("periodo", datetime.now().strftime("%Y")))
    guardar_dj1847_periodo(clean, periodo, data)
    return jsonify({"ok": True})


@app.route("/api/folios/<rut>", methods=["GET"])
@login_required
def api_folios_get(rut):
    clean = _clean_rut(rut)
    df = get_folios_usados(clean)
    if df.empty:
        return jsonify([])
    return jsonify(df.fillna("").to_dict(orient="records"))


@app.route("/api/exportar_dj1847_csv/<rut>", methods=["GET"])
@login_required
def api_exportar_dj1847_csv(rut):
    clean = _clean_rut(rut)
    fecha = request.args.get("fecha", datetime.now().strftime("%Y-%m-%d"))
    periodo = request.args.get("periodo", datetime.now().strftime("%Y"))
    
    # Obtener datos de la Sección B
    periodo_data = get_dj1847_periodo(clean, periodo)
    if not periodo_data:
        emp = get_empresa(clean) or {}
        periodo_data = {
            "actividad_economica": emp.get("actividad_principal", ""),
            "entidad_supervisora": emp.get("entidad_supervisora", "NO APLICA"),
            "anio_ifrs": emp.get("anio_ifrs", 0) or 0,
            "folio_ini": "",
            "folio_fin": "",
            "ajustes_rli": 2,
        }
    
    # Obtener datos de la Sección C
    data = _build_dj1847_data(clean, fecha)
    filas = data.get("filas", [])
    
    # Generar CSV con separador ;
    import csv
    import io
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";", lineterminator="\n")
    
    # Fila Sección B (indicador = 1)
    sec_b = [
        "1",
        str(periodo_data.get("actividad_economica", "")),
        str(periodo_data.get("entidad_supervisora", "NO APLICA")),
        str(periodo_data.get("anio_ifrs", 0) or 0),
        str(periodo_data.get("folio_ini", "")),
        str(periodo_data.get("folio_fin", "")),
        str(periodo_data.get("ajustes_rli", 2)),
        "", "", "", "", "", ""
    ]
    writer.writerow(sec_b)
    
    # Filas Sección C (indicador = 2)
    for row in filas:
        sec_c = [
            "2",
            "", "", "", "", "", "",  # columnas de Sección B vacías
            str(row.get("cuenta", "")),
            str(row.get("cuenta_sii", "")),
            str(row.get("nombre", "")),
            str(int(row.get("debe", 0) or 0)),
            str(int(row.get("haber", 0) or 0)),
            str(int(row.get("saldo_deudor", 0) or 0)),
            str(int(row.get("saldo_acreedor", 0) or 0)),
            str(int(row.get("activo", 0) or 0)),
            str(int(row.get("pasivo", 0) or 0)),
            str(int(row.get("perdida", 0) or 0)),
            str(int(row.get("ganancia", 0) or 0)),
            str(row.get("cod_f22", "")),
            str(int(row.get("valor_tributario", 0) or 0)),
        ]
        writer.writerow(sec_c)
    
    # Guardar folio como usado
    folio_ini = periodo_data.get("folio_ini")
    folio_fin = periodo_data.get("folio_fin")
    if folio_ini and folio_fin:
        try:
            guardar_folio_usado(clean, "1847", periodo, int(folio_ini), int(folio_fin))
        except Exception:
            pass
    
    csv_content = output.getvalue()
    output.close()
    
    return send_file(
        io.BytesIO(csv_content.encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"DJ1847_{clean}_{periodo}.csv"
    )


@app.route("/api/exportar_dj1847/<rut>", methods=["GET"])
@login_required
def api_exportar_dj1847(rut):
    clean = _clean_rut(rut)
    fecha = request.args.get("fecha", datetime.now().strftime("%Y-%m-%d"))
    data = _build_dj1847_data(clean, fecha)
    df = pd.DataFrame(data.get("filas", []), columns=DJ1847_COLUMNAS)
    df = df.rename(columns=DJ1847_HEADERS)
    output = f"DJ1847_{clean}_{fecha}.xlsx"
    df.to_excel(output, index=False)
    return send_file(output, as_attachment=True)


# ============================================================
# API - DJ1926
# ============================================================
@app.route("/api/dj1926/<rut>", methods=["GET"])
@login_required
def api_dj1926(rut):
    clean = _clean_rut(rut)
    fecha = request.args.get("fecha", datetime.now().strftime("%Y-%m-%d"))
    balance = get_balance_data(clean, fecha, "TRIBUTARIO")
    resultado_financiero = 0.0
    if not balance.empty:
        resultado_financiero = float(balance["ganancia"].sum() - balance["perdida"].sum())
    aj = get_ajustes_tributarios(clean)
    if aj.empty:
        aj = pd.DataFrame(columns=["codigo_sii", "descripcion", "monto", "tipo_ajuste", "cuenta_afectada"])
    aj["monto"] = pd.to_numeric(aj["monto"], errors="coerce").fillna(0)
    agregados = float(aj[aj["tipo_ajuste"] == 1]["monto"].sum())
    deducciones = float(aj[aj["tipo_ajuste"] == 2]["monto"].sum())
    ded_e = float(aj[aj["tipo_ajuste"] == 4]["monto"].sum())
    res_fin = float(aj[aj["tipo_ajuste"] == 9]["monto"].sum())
    if res_fin == 0:
        res_fin = resultado_financiero
    rli = res_fin + agregados - deducciones - ded_e
    return jsonify({
        "seccion_b": aj.fillna("").to_dict(orient="records"),
        "resumen": {
            "resultado_financiero": res_fin,
            "agregados": agregados,
            "deducciones": deducciones,
            "ded_e": ded_e,
            "rli": rli,
        }
    })


@app.route("/api/exportar_dj1926/<rut>", methods=["GET"])
@login_required
def api_exportar_dj1926(rut):
    clean = _clean_rut(rut)
    fecha = request.args.get("fecha", datetime.now().strftime("%Y-%m-%d"))
    data = api_dj1926(clean)
    if hasattr(data, "get_json"):
        data = data.get_json()
    output = f"DJ1926_{clean}_{fecha}.xlsx"
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(data.get("seccion_b", [])).to_excel(writer, sheet_name="Seccion B", index=False)
        pd.DataFrame([data.get("resumen", {})]).to_excel(writer, sheet_name="Resumen", index=False)
    return send_file(output, as_attachment=True)


# ============================================================
# API - LEDGER (para detalle de cuenta)
# ============================================================
@app.route("/api/ledger/<rut>", methods=["GET"])
@login_required
def api_ledger(rut):
    clean = _clean_rut(rut)
    df = get_ledger(clean)
    if df.empty:
        return jsonify([])
    cuenta = request.args.get("cuenta")
    if cuenta:
        df = df[df["cuenta"] == cuenta]
    return jsonify(df.fillna("").head(1000).to_dict(orient="records"))


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    init_auth_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("DEBUG", "True").lower() == "true")
