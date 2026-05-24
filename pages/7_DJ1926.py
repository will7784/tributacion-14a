import streamlit as st
import pandas as pd
from core.db import get_ajustes_tributarios
from core.balance import get_balance_data

st.set_page_config(page_title="DJ1926", layout="wide")
st.title("📑 Declaración Jurada 1926 - Base Imponible")

emp = st.session_state.get("empresa")
if not emp:
    st.warning("Selecciona una empresa en la página principal.")
    st.stop()

st.info(f"Empresa: **{emp['nombre']}** ({emp['rut']})")

fecha_hasta = st.date_input("Fecha cierre", value=pd.Timestamp("2024-12-31"))
if st.button("Generar DJ1926", type="primary"):
    # Resultado según balance tributario
    balance = get_balance_data(emp["rut"], fecha_hasta.strftime("%Y-%m-%d"), "TRIBUTARIO")
    resultado_financiero = 0
    if not balance.empty:
        resultado_financiero = balance["ganancia"].sum() - balance["perdida"].sum()

    # Ajustes
    aj = get_ajustes_tributarios(emp["rut"])
    if aj.empty:
        aj = pd.DataFrame(columns=["codigo_sii", "descripcion", "monto", "tipo_ajuste", "cuenta_afectada"])

    aj["monto"] = pd.to_numeric(aj["monto"], errors="coerce").fillna(0)

    # Sección B
    st.subheader("Sección B: Ajustes en la determinación de la Base Imponible")
    seccion_b = aj.copy()
    seccion_b["Tipo"] = seccion_b["tipo_ajuste"].map({1: "Agregado", 2: "Deducción", 4: "Deducción Letra E", 9: "Resultado Financiero"})
    st.dataframe(seccion_b[["codigo_sii", "descripcion", "monto", "Tipo"]], use_container_width=True)

    # Cuadro resumen
    agregados = aj[aj["tipo_ajuste"] == 1]["monto"].sum()
    deducciones = aj[aj["tipo_ajuste"] == 2]["monto"].sum()
    ded_e = aj[aj["tipo_ajuste"] == 4]["monto"].sum()
    res_fin = aj[aj["tipo_ajuste"] == 9]["monto"].sum()
    if res_fin == 0:
        res_fin = resultado_financiero

    rli = res_fin + agregados - deducciones - ded_e

    st.subheader("Cuadro Resumen Sección B")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Resultado Financiero [9]", f"{res_fin:,.0f}")
    c2.metric("Total Agregados [1]", f"{agregados:,.0f}")
    c3.metric("Total Deducciones [2]", f"{deducciones:,.0f}")
    c4.metric("Ded. Letra E [4]", f"{ded_e:,.0f}")
    c5.metric("RLI / Pérdida Tributaria", f"{rli:,.0f}")

    # Exportar
    output = f"DJ1926_{emp['rut']}_{fecha_hasta.strftime('%Y%m%d')}.xlsx"
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        seccion_b.to_excel(writer, sheet_name="Seccion B", index=False)
        resumen = pd.DataFrame({
            "Concepto": ["Resultado Financiero", "Agregados", "Deducciones", "Deducción Letra E", "RLI"],
            "Monto": [res_fin, agregados, deducciones, ded_e, rli]
        })
        resumen.to_excel(writer, sheet_name="Resumen", index=False)
    with open(output, "rb") as f:
        st.download_button("📥 Descargar Excel DJ1926", f, file_name=output)
