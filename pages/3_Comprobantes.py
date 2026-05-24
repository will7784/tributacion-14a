import streamlit as st
import pandas as pd
from datetime import datetime
from core.db import get_plan_cuentas, guardar_comprobante, get_comprobantes, get_comprobante_lineas

st.set_page_config(page_title="Comprobantes", layout="wide")
st.title("📝 Comprobantes Contables / Tributarios")

emp = st.session_state.get("empresa")
if not emp:
    st.warning("Selecciona una empresa en la página principal.")
    st.stop()

plan = get_plan_cuentas(emp["rut"])
if plan.empty:
    st.info("No hay plan de cuentas. Importa un ledger primero.")
    st.stop()

cuentas_options = sorted(plan["cuenta_local"].tolist())

tab1, tab2 = st.tabs(["Nuevo Comprobante", "Ver Comprobantes"])

with tab1:
    with st.form("nuevo_comprobante"):
        st.subheader("Cabecera")
        col1, col2, col3 = st.columns(3)
        with col1:
            fecha = st.date_input("Fecha", value=datetime.today())
        with col2:
            numero = st.text_input("Número", value="")
        with col3:
            tipo_norma = st.radio("Norma", ["CYT", "T"], index=0, horizontal=True, help="CYT = Contable y Tributario (default). T = Solo Tributario.")
        glosa = st.text_input("Glosa")

        st.subheader("Líneas")
        lineas = []
        for i in range(5):
            cols = st.columns([3, 2, 2, 3])
            with cols[0]:
                cuenta = st.selectbox(f"Cuenta {i+1}", [""] + cuentas_options, key=f"cuenta_{i}")
            with cols[1]:
                debe = st.number_input(f"Debe {i+1}", min_value=0.0, key=f"debe_{i}")
            with cols[2]:
                haber = st.number_input(f"Haber {i+1}", min_value=0.0, key=f"haber_{i}")
            with cols[3]:
                concepto = st.text_input(f"Concepto {i+1}", key=f"concepto_{i}")
            if cuenta:
                lineas.append({"cuenta": cuenta, "debe": debe, "haber": haber, "concepto": concepto, "tipo_norma": tipo_norma})

        submitted = st.form_submit_button("Guardar Comprobante")
        if submitted:
            total_debe = sum(l["debe"] for l in lineas)
            total_haber = sum(l["haber"] for l in lineas)
            if abs(total_debe - total_haber) > 0.01:
                st.error(f"El comprobante no cuadra: Debe {total_debe:,.0f} != Haber {total_haber:,.0f}")
            elif not lineas:
                st.error("Ingresa al menos una línea.")
            else:
                comp_id = guardar_comprobante(emp["rut"], fecha.strftime("%Y-%m-%d"), glosa, tipo_norma, numero, lineas)
                st.success(f"Comprobante {numero or comp_id} guardado ({tipo_norma}).")

with tab2:
    comps = get_comprobantes(emp["rut"])
    if comps.empty:
        st.info("No hay comprobantes registrados.")
    else:
        st.dataframe(comps, use_container_width=True)
        comp_sel = st.selectbox("Ver líneas del comprobante", comps["id"].tolist(), format_func=lambda x: f"{x} - {comps[comps['id']==x]['glosa'].values[0]} ({comps[comps['id']==x]['tipo_norma'].values[0]})")
        if comp_sel:
            lineas_df = get_comprobante_lineas(emp["rut"], comp_sel)
            st.dataframe(lineas_df, use_container_width=True)
