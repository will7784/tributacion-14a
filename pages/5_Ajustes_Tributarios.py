import streamlit as st
import pandas as pd
from core.db import get_ajustes_tributarios, guardar_ajustes_tributarios
from core.plan_cuentas import cargar_plan_sii

st.set_page_config(page_title="Ajustes Tributarios", layout="wide")
st.title("⚖️ Ajustes Tributarios - DJ1926")

emp = st.session_state.get("empresa")
if not emp:
    st.warning("Selecciona una empresa en la página principal.")
    st.stop()

sii = cargar_plan_sii()
ajustes_sii = sii[sii["tipo"] == "AJUSTE"].copy()
options_ajuste = [""] + [f"{row['cuenta_sii']} - {row['nombre']}" for _, row in ajustes_sii.iterrows()]

tab_guiado, tab_manual, tab_resumen = st.tabs(["Formulario Guiado", "Ajustes Manuales", "Resumen RLI"])

with tab_guiado:
    st.subheader("Agregar ajuste guiado")
    with st.form("ajuste_guiado"):
        sel = st.selectbox("Tipo de ajuste", options_ajuste)
        monto = st.number_input("Monto", min_value=0.0)
        tipo = st.selectbox("Tipo", [(1, "Agregado"), (2, "Deducción"), (4, "Deducción Letra E"), (9, "Resultado Financiero")], format_func=lambda x: x[1])
        submitted = st.form_submit_button("Agregar")
        if submitted and sel:
            codigo = sel.split(" - ")[0]
            descripcion = sel.split(" - ")[1]
            df_act = get_ajustes_tributarios(emp["rut"])
            nuevo = pd.DataFrame([{"codigo_sii": codigo, "descripcion": descripcion, "monto": monto, "tipo_ajuste": tipo[0], "cuenta_afectada": ""}])
            df_act = pd.concat([df_act, nuevo], ignore_index=True)
            guardar_ajustes_tributarios(emp["rut"], df_act)
            st.success("Ajuste agregado.")

with tab_manual:
    st.subheader("Editar ajustes manualmente")
    df = get_ajustes_tributarios(emp["rut"])
    if df.empty:
        df = pd.DataFrame(columns=["codigo_sii", "descripcion", "monto", "tipo_ajuste", "cuenta_afectada"])
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True)
    if st.button("💾 Guardar ajustes", type="primary"):
        guardar_ajustes_tributarios(emp["rut"], edited)
        st.success("Ajustes guardados.")

with tab_resumen:
    df = get_ajustes_tributarios(emp["rut"])
    if df.empty:
        st.info("No hay ajustes registrados.")
    else:
        df["monto"] = pd.to_numeric(df["monto"], errors="coerce").fillna(0)
        agregados = df[df["tipo_ajuste"] == 1]["monto"].sum()
        deducciones = df[df["tipo_ajuste"] == 2]["monto"].sum()
        ded_e = df[df["tipo_ajuste"] == 4]["monto"].sum()
        res_fin = df[df["tipo_ajuste"] == 9]["monto"].sum()
        rli = res_fin + agregados - deducciones - ded_e

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Resultado Financiero", f"{res_fin:,.0f}")
        c2.metric("Agregados", f"{agregados:,.0f}")
        c3.metric("Deducciones", f"{deducciones:,.0f}")
        c4.metric("Ded. Letra E", f"{ded_e:,.0f}")
        c5.metric("RLI Estimada", f"{rli:,.0f}")

        st.dataframe(df, use_container_width=True)
