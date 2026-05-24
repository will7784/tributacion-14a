import streamlit as st
import pandas as pd
from core.db import get_plan_cuentas
from core.balance import get_balance_data
from core.plan_cuentas import cargar_plan_sii

st.set_page_config(page_title="DJ1847", layout="wide")
st.title("📑 Declaración Jurada 1847 - Balance de 8 Columnas")

emp = st.session_state.get("empresa")
if not emp:
    st.warning("Selecciona una empresa en la página principal.")
    st.stop()

st.info(f"Empresa: **{emp['nombre']}** ({emp['rut']})")

fecha_hasta = st.date_input("Fecha cierre balance", value=pd.Timestamp("2024-12-31"))
if st.button("Generar DJ1847", type="primary"):
    balance = get_balance_data(emp["rut"], fecha_hasta.strftime("%Y-%m-%d"), "TRIBUTARIO")
    plan = get_plan_cuentas(emp["rut"])
    sii = cargar_plan_sii()

    if balance.empty:
        st.warning("No hay balance tributario. Importa datos y crea ajustes.")
    else:
        # Quitar filas de totales
        bal = balance[~balance["_es_total"]].copy()
        # Merge con plan local para traer cuenta_sii
        bal = bal.merge(plan[["cuenta_local", "cuenta_sii"]], left_on="cuenta", right_on="cuenta_local", how="left")
        bal["cuenta_sii"] = bal["cuenta_sii"].fillna("")

        # Valor tributario: por MVP, igual al valor financiero para activos/pasivos
        def valor_tributario(row):
            if row["tipo"] in ("ACTIVO", "PASIVO"):
                return row["activo"] - row["pasivo"]
            return 0

        # Determinar tipo desde plan
        tipo_map = plan.set_index("cuenta_local")["tipo"].to_dict()
        bal["tipo"] = bal["cuenta"].map(tipo_map).fillna("OTRO")

        bal["valor_tributario"] = bal.apply(valor_tributario, axis=1)

        # Concepto resultado financiero (simplificado: basado en primer dígito)
        concepto_map = {
            "INGRESO": "1657",
            "COSTO": "1661",
            "GASTO": "1662",
        }
        bal["concepto"] = bal["tipo"].map(concepto_map).fillna("")

        # Columnas finales DJ1847
        final = pd.DataFrame({
            "N°": range(1, len(bal) + 1),
            "Id. plan cuentas": bal["cuenta"],
            "Id. cuenta SII": bal["cuenta_sii"],
            "Nombre cuenta": bal["nombre_cuenta"],
            "Débitos": bal["debe"],
            "Créditos": bal["haber"],
            "Saldo Deudor": bal["saldo_deudor"],
            "Saldo Acreedor": bal["saldo_acreedor"],
            "Activo": bal["activo"],
            "Pasivo": bal["pasivo"],
            "Pérdidas": bal["perdida"],
            "Ganancias": bal["ganancia"],
            "Concepto": bal["concepto"],
            "Valor Tributario": bal["valor_tributario"],
        })

        st.dataframe(final, use_container_width=True)

        # Totales
        st.subheader("Cuadro Resumen")
        cols = st.columns(8)
        cols[0].metric("Total Débitos", f"{final['Débitos'].sum():,.0f}")
        cols[1].metric("Total Créditos", f"{final['Créditos'].sum():,.0f}")
        cols[2].metric("Total Saldo Deudor", f"{final['Saldo Deudor'].sum():,.0f}")
        cols[3].metric("Total Saldo Acreedor", f"{final['Saldo Acreedor'].sum():,.0f}")
        cols[4].metric("Total Activo", f"{final['Activo'].sum():,.0f}")
        cols[5].metric("Total Pasivo", f"{final['Pasivo'].sum():,.0f}")
        cols[6].metric("Total Pérdidas", f"{final['Pérdidas'].sum():,.0f}")
        cols[7].metric("Total Ganancias", f"{final['Ganancias'].sum():,.0f}")

        output = f"DJ1847_{emp['rut']}_{fecha_hasta.strftime('%Y%m%d')}.xlsx"
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            final.to_excel(writer, sheet_name="Seccion C", index=False)
        with open(output, "rb") as f:
            st.download_button("📥 Descargar Excel DJ1847", f, file_name=output)
