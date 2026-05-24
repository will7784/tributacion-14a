import streamlit as st
import pandas as pd
from datetime import datetime
from core.db import get_plan_cuentas
from core.balance import get_balance_data

st.set_page_config(page_title="Balance", layout="wide")
st.title("📊 Balance de 8 Columnas")

emp = st.session_state.get("empresa")
if not emp:
    st.warning("Selecciona una empresa en la página principal.")
    st.stop()

st.info(f"Empresa: **{emp['nombre']}** ({emp['rut']})")

col1, col2 = st.columns([1, 3])
with col1:
    fecha_hasta = st.date_input("Fecha hasta", value=datetime(2024, 12, 31))
    norma = st.radio("Norma", ["CONTABLE", "TRIBUTARIO"], index=0)
    if st.button("🔄 Calcular", type="primary"):
        st.session_state["balance_calculado"] = True
        st.session_state["balance_fecha"] = fecha_hasta.strftime("%Y-%m-%d")
        st.session_state["balance_norma"] = norma

if st.session_state.get("balance_calculado"):
    df = get_balance_data(emp["rut"], st.session_state["balance_fecha"], st.session_state["balance_norma"])
    if df.empty:
        st.warning("No hay datos para la fecha/norma seleccionada.")
    else:
        st.subheader(f"Balance {st.session_state['balance_norma']} al {st.session_state['balance_fecha']}")

        # Separar filas de totales
        filas_normales = df[~df["_es_total"]].copy()
        filas_totales = df[df["_es_total"]].copy()

        # Formato miles
        def fmt(n):
            if pd.isna(n) or n == 0:
                return ""
            s = f"{abs(int(n)):,}".replace(",", ".")
            return f"({s})" if n < 0 else s

        def aplicar_fmt(df_in):
            df_out = df_in.copy()
            for c in ["debe", "haber", "saldo_deudor", "saldo_acreedor", "activo", "pasivo", "perdida", "ganancia"]:
                df_out[c] = df_out[c].apply(fmt)
            return df_out

        st.dataframe(
            aplicar_fmt(filas_normales)[["n", "cuenta", "nombre_cuenta", "debe", "haber", "saldo_deudor", "saldo_acreedor", "activo", "pasivo", "perdida", "ganancia"]],
            use_container_width=True
        )

        if not filas_totales.empty:
            st.subheader("Totales")
            st.dataframe(
                aplicar_fmt(filas_totales)[["nombre_cuenta", "debe", "haber", "saldo_deudor", "saldo_acreedor", "activo", "pasivo", "perdida", "ganancia"]],
                use_container_width=True
            )

        # Exportar
        if st.button("📥 Exportar a Excel"):
            output = f"balance_{st.session_state['balance_norma']}_{st.session_state['balance_fecha']}.xlsx"
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                df.drop(columns=["_es_total"]).to_excel(writer, sheet_name="Balance", index=False)
            with open(output, "rb") as f:
                st.download_button("Descargar Excel", f, file_name=output)
