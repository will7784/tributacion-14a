import streamlit as st
import pandas as pd
from parsers.csv_import import parse_csv
from parsers.excel_import import parse_excel
from core.db import init_db, guardar_ledger, limpiar_ledger, get_plan_cuentas, guardar_plan_cuentas
from core.plan_cuentas import homologar_plan, inferir_tipo

st.set_page_config(page_title="Importar Ledger", layout="wide")
st.title("📥 Importar Libro Mayor")

emp = st.session_state.get("empresa")
if not emp:
    st.warning("Selecciona una empresa en la página principal.")
    st.stop()

init_db(emp["rut"])

st.info(f"Empresa: **{emp['nombre']}** ({emp['rut']})")

uploaded = st.file_uploader("Arrastra el archivo CSV o Excel del libro mayor", type=["csv", "xlsx", "xls"])

if uploaded:
    ext = uploaded.name.split(".")[-1].lower()
    bytes_data = uploaded.getvalue()
    try:
        if ext == "csv":
            df_raw = parse_csv(bytes_data)
        else:
            df_raw = parse_excel(bytes_data)
        st.success(f"Archivo leído: {len(df_raw)} filas, {len(df_raw.columns)} columnas.")
        st.write("Vista previa:")
        st.dataframe(df_raw.head(20), use_container_width=True)

        # Normalizar nombres de columnas para detección robusta
        def _norm_col(name: str) -> str:
            return name.lower().strip().replace("$", "").replace(".", "").replace("  ", " ")

        col_map = {}
        for c in df_raw.columns:
            cl = _norm_col(c)
            # También versión sin espacios internos
            cl_nospace = cl.replace(" ", "")
            if cl in ("cuenta", "codigo", "code", "id cuenta") or cl_nospace in ("cuenta", "codigo", "code", "idcuenta"):
                col_map["cuenta"] = c
            elif cl in ("nombre", "descripcion", "glosa", "nombre cuenta", "nombre de cuenta") or cl_nospace in ("nombre", "descripcion", "glosa", "nombrecuenta"):
                col_map["nombre"] = c
            elif cl in ("debe", "debito", "cargo", "debe $") or cl_nospace in ("debe", "debito", "cargo", "debe$"):
                col_map["debe"] = c
            elif cl in ("haber", "credito", "abono", "haber $") or cl_nospace in ("haber", "credito", "abono", "haber$"):
                col_map["haber"] = c
            elif cl in ("fecha", "date") or cl_nospace in ("fecha", "date"):
                col_map["fecha"] = c
            elif cl in ("comprobante", "folio", "asiento", "número", "nro comprobante") or cl_nospace in ("comprobante", "folio", "asiento", "numero", "nrocomprobante"):
                col_map["comprobante"] = c
            elif cl in ("tipo comprobante", "tipocomprobante", "tipo", "tipo de comprobante") or cl_nospace in ("tipocomprobante", "tipodecomprobante"):
                col_map["tipo_comprobante"] = c
            elif cl in ("rut ficha", "rut", "rutficha", "rut cliente") or cl_nospace in ("rutficha", "rutcliente"):
                col_map["rut_ficha"] = c
            elif cl in ("rzn social ficha", "razon social", "nombre ficha", "razón social", "nombre cliente") or cl_nospace in ("rznsocialficha", "razonsocial", "nombreficha", "nombrecliente"):
                col_map["razon_social"] = c
            elif cl in ("comentario linea", "glosa linea", "concepto", "glosa") or cl_nospace in ("comentariolinea", "glosalinea", "concepto", "glosa"):
                col_map["concepto"] = c
            elif cl in ("documento", "folio doc", "nro doc", "número documento") or cl_nospace in ("documento", "foliodoc", "nrodoc", "numerodocumento"):
                col_map["documento"] = c
            elif cl in ("fecha venc", "vencimiento", "fecha de vencimiento") or cl_nospace in ("fechavenc", "vencimiento", "fechadevencimiento"):
                col_map["fecha_venc"] = c
            elif cl in ("unidad negocio", "unidad_de_negocio", "proyecto", "unidad de negocio") or cl_nospace in ("unidadnegocio", "unidaddenegocio"):
                col_map["unidad_negocio"] = c
            elif cl in ("tipo movimiento", "tipomovimiento", "tipo de movimiento") or cl_nospace in ("tipomovimiento", "tipodemovimiento"):
                col_map["tipo_movimiento"] = c
            elif cl in ("número movimiento", "numeromovimiento", "nro mov", "número de movimiento") or cl_nospace in ("numeromovimiento", "nromov", "numerodemovimiento"):
                col_map["numero_movimiento"] = c

        # Mapeo manual si no detectó algo crítico
        faltantes = []
        for req in ["cuenta", "debe", "haber"]:
            if req not in col_map:
                faltantes.append(req)
        if faltantes:
            st.warning(f"No detecté columnas para: {', '.join(faltantes)}. Selección manual:")
            for f in faltantes:
                col_map[f] = st.selectbox(f"Columna para '{f}'", df_raw.columns, key=f"map_{f}")

        # Normalizar DataFrame
        df = pd.DataFrame()
        cuenta_raw = df_raw[col_map.get("cuenta", df_raw.columns[0])].astype(str).str.strip()
        # Extraer código de cuenta si viene junto al nombre (ej: "1.01.01.01 Caja")
        df["cuenta"] = cuenta_raw.str.extract(r"^\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", expand=False).fillna(cuenta_raw)
        # Nombre: si hay código al inicio, tomar el resto; sino buscar columna nombre aparte
        nombre_col = col_map.get("nombre")
        cuenta_col_detected = col_map.get("cuenta", df_raw.columns[0])
        if nombre_col and nombre_col != cuenta_col_detected:
            df["nombre_cuenta"] = df_raw[nombre_col].astype(str).str.strip()
        else:
            df["nombre_cuenta"] = cuenta_raw.str.replace(r"^\s*[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\s*", "", regex=True).str.strip()
            # Si quedó vacío, usar el código como nombre
            df.loc[df["nombre_cuenta"] == "", "nombre_cuenta"] = df["cuenta"]
        df["debe"] = pd.to_numeric(df_raw[col_map.get("debe", "debe")], errors="coerce").fillna(0)
        df["haber"] = pd.to_numeric(df_raw[col_map.get("haber", "haber")], errors="coerce").fillna(0)
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

        st.subheader("Vista previa normalizada")
        st.dataframe(df.head(20), use_container_width=True)

        if st.button("💾 Guardar en base de datos", type="primary"):
            limpiar_ledger(emp["rut"])
            guardar_ledger(df, emp["rut"])
            st.success(f"Guardado {len(df)} registros en la base de datos.")

            # Homologar plan de cuentas automáticamente
            cuentas_unicas = df[["cuenta", "nombre_cuenta"]].drop_duplicates().rename(columns={"cuenta": "cuenta", "nombre": "nombre_cuenta"})
            # Usar solo cuenta y nombre
            cuentas_import = pd.DataFrame({"cuenta": df["cuenta"].unique()})
            # Buscar nombre más frecuente por cuenta
            nombre_por_cuenta = df.groupby("cuenta")["nombre_cuenta"].first().reset_index()
            cuentas_import = nombre_por_cuenta.rename(columns={"nombre_cuenta": "nombre"})

            homologado = homologar_plan(cuentas_import)

            # Merge con plan existente para no pisar asignaciones manuales previas
            plan_existente = get_plan_cuentas(emp["rut"])
            if not plan_existente.empty:
                plan_existente = plan_existente.set_index("cuenta_local")
                for idx, row in homologado.iterrows():
                    cl = row["cuenta_local"]
                    if cl in plan_existente.index and plan_existente.loc[cl, "cuenta_sii"] != "":
                        homologado.at[idx, "cuenta_sii"] = plan_existente.loc[cl, "cuenta_sii"]

            # Guardar plan de cuentas
            guardar_plan_cuentas(emp["rut"], homologado)
            st.success("Plan de cuentas homologado generado. Revisa la página 'Plan de Cuentas' para completar el mapeo SII.")

    except Exception as e:
        st.error(f"Error al procesar el archivo: {e}")
