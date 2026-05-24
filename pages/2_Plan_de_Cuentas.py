import streamlit as st
import pandas as pd
from core.db import get_plan_cuentas, guardar_plan_cuentas
from core.plan_cuentas import cargar_plan_sii

st.set_page_config(page_title="Plan de Cuentas", layout="wide")
st.title("📋 Plan de Cuentas - Homologación SII")

emp = st.session_state.get("empresa")
if not emp:
    st.warning("Selecciona una empresa en la página principal.")
    st.stop()

st.info(f"Empresa: **{emp['nombre']}** ({emp['rut']})")

plan = get_plan_cuentas(emp["rut"])
if plan.empty:
    st.info("No hay plan de cuentas. Importa un ledger primero en la página 'Importar'.")
    st.stop()

sii = cargar_plan_sii()
sii_options = [""] + sorted([f"{row['cuenta_sii']} - {row['nombre']}" for _, row in sii.iterrows()])

st.write(f"Total cuentas: {len(plan)}")

# Filtro
filtro = st.text_input("Buscar cuenta o nombre")
df = plan.copy()
if filtro:
    mask = df["cuenta_local"].str.contains(filtro, case=False, na=False) | df["nombre_local"].str.contains(filtro, case=False, na=False)
    df = df[mask]

# Editor
st.subheader("Asignar cuenta SII")
edit_df = st.data_editor(
    df[["cuenta_local", "nombre_local", "cuenta_base", "cuenta_sii", "tipo"]],
    column_config={
        "cuenta_local": st.column_config.TextColumn("Cuenta Cliente", disabled=True),
        "nombre_local": st.column_config.TextColumn("Nombre Cliente", disabled=True),
        "cuenta_base": st.column_config.TextColumn("Cuenta Base", disabled=True),
        "cuenta_sii": st.column_config.SelectboxColumn("Cuenta SII", options=sii_options),
        "tipo": st.column_config.TextColumn("Tipo", disabled=True),
    },
    use_container_width=True,
    num_rows="dynamic",
    key="plan_editor"
)

if st.button("💾 Guardar cambios", type="primary"):
    # Merge con las columnas que no están en el editor
    plan_updated = plan.set_index("cuenta_local")
    for _, row in edit_df.iterrows():
        cl = row["cuenta_local"]
        if cl in plan_updated.index:
            plan_updated.at[cl, "cuenta_sii"] = row["cuenta_sii"]
    plan_updated = plan_updated.reset_index()
    guardar_plan_cuentas(emp["rut"], plan_updated)
    st.success("Plan de cuentas actualizado.")
