import pandas as pd
import sqlite3
from pathlib import Path


def get_balance_data(rut: str, fecha_hasta: str, norma: str = "CONTABLE") -> pd.DataFrame:
    """
    norma: 'CONTABLE' | 'TRIBUTARIO'
    Retorna DataFrame con columnas del balance de 8 columnas por cuenta,
    incluyendo la fila de Resultado del Ejercicio para cuadrar.
    """
    db_path = Path(f"{rut.replace('.', '').replace('-', '').upper()}_14a.db")
    if not db_path.exists():
        return pd.DataFrame()

    conn = sqlite3.connect(db_path)

    # Ledger importado + comprobantes CYT
    query_ledger = """
        SELECT cuenta, nombre_cuenta, SUM(debe) as debe, SUM(haber) as haber
        FROM ledger
        WHERE fecha <= ?
        GROUP BY cuenta, nombre_cuenta
    """
    df_ledger = pd.read_sql(query_ledger, conn, params=(fecha_hasta,))

    # Comprobantes CYT (afectan ambas normas)
    query_cyt = """
        SELECT l.cuenta, '' as nombre_cuenta, SUM(l.debe) as debe, SUM(l.haber) as haber
        FROM comprobante_lineas l
        JOIN comprobantes c ON c.id = l.comprobante_id
        WHERE c.fecha <= ? AND l.tipo_norma = 'CYT'
        GROUP BY l.cuenta
    """
    df_cyt = pd.read_sql(query_cyt, conn, params=(fecha_hasta,))

    # Comprobantes solo T (solo tributario)
    df_t = pd.DataFrame(columns=["cuenta", "nombre_cuenta", "debe", "haber"])
    if norma == "TRIBUTARIO":
        query_t = """
            SELECT l.cuenta, '' as nombre_cuenta, SUM(l.debe) as debe, SUM(l.haber) as haber
            FROM comprobante_lineas l
            JOIN comprobantes c ON c.id = l.comprobante_id
            WHERE c.fecha <= ? AND l.tipo_norma = 'T'
            GROUP BY l.cuenta
        """
        df_t = pd.read_sql(query_t, conn, params=(fecha_hasta,))

    conn.close()

    # Unificar
    frames = [f for f in [df_ledger, df_cyt, df_t] if not f.empty]
    if not frames:
        return pd.DataFrame()
    df_all = pd.concat(frames, ignore_index=True)

    df_all["debe"] = pd.to_numeric(df_all["debe"], errors="coerce").fillna(0)
    df_all["haber"] = pd.to_numeric(df_all["haber"], errors="coerce").fillna(0)

    grouped = df_all.groupby(["cuenta", "nombre_cuenta"], as_index=False).agg({"debe": "sum", "haber": "sum"})

    grouped["saldo"] = grouped["debe"] - grouped["haber"]
    grouped["saldo_deudor"] = grouped["saldo"].apply(lambda x: x if x > 0 else 0)
    grouped["saldo_acreedor"] = grouped["saldo"].apply(lambda x: abs(x) if x < 0 else 0)

    # Clasificación por primer dígito
    def clasificar(row):
        cuenta = str(row["cuenta"]).strip()
        primer = cuenta[0] if cuenta else ""
        saldo = row["saldo"]
        if primer in ("1", "2"):
            # Activos y Pasivos (incluye patrimonio que va en pasivo)
            if saldo >= 0:
                return pd.Series([saldo, 0, 0, 0])
            else:
                return pd.Series([0, abs(saldo), 0, 0])
        else:
            # Resultados: 3.x = ingresos, 4.x = egresos
            # Saldo = debe - haber
            # Para ingresos (3.x): normalmente haber > debe → saldo < 0 → Ganancia
            # Para egresos (4.x): normalmente debe > haber → saldo > 0 → Pérdida
            if saldo >= 0:
                return pd.Series([0, 0, saldo, 0])
            else:
                return pd.Series([0, 0, 0, abs(saldo)])

    grouped[["activo", "pasivo", "perdida", "ganancia"]] = grouped.apply(clasificar, axis=1)

    # Ordenar por cuenta
    grouped = grouped.sort_values("cuenta").reset_index(drop=True)
    grouped["n"] = grouped.index + 1

    # Calcular totales de cuentas
    t_debe = grouped["debe"].sum()
    t_haber = grouped["haber"].sum()
    t_activo = grouped["activo"].sum()
    t_pasivo = grouped["pasivo"].sum()
    t_perdida = grouped["perdida"].sum()
    t_ganancia = grouped["ganancia"].sum()

    # Resultado del ejercicio: diferencia entre ganancias y pérdidas
    resultado = t_ganancia - t_perdida

    # Fila de resultado para cuadrar el balance
    # Si hay ganancia neta (resultado > 0): va a Pasivo y a Pérdida
    # Si hay pérdida neta (resultado < 0): va a Activo y a Ganancia
    res_activo = abs(resultado) if resultado < 0 else 0
    res_pasivo = abs(resultado) if resultado > 0 else 0
    res_perdida = abs(resultado) if resultado > 0 else 0
    res_ganancia = abs(resultado) if resultado < 0 else 0

    fila_resultado = pd.DataFrame([{
        "n": len(grouped) + 1,
        "cuenta": "",
        "nombre_cuenta": "Resultado del Ejercicio",
        "debe": 0,
        "haber": 0,
        "saldo_deudor": 0,
        "saldo_acreedor": 0,
        "activo": res_activo,
        "pasivo": res_pasivo,
        "perdida": res_perdida,
        "ganancia": res_ganancia,
        "saldo": 0,
        "_es_total": True,
    }])

    # Fila de sumas totales
    fila_sumas = pd.DataFrame([{
        "n": len(grouped) + 2,
        "cuenta": "",
        "nombre_cuenta": "Sumas Totales",
        "debe": t_debe,
        "haber": t_haber,
        "saldo_deudor": grouped["saldo_deudor"].sum(),
        "saldo_acreedor": grouped["saldo_acreedor"].sum(),
        "activo": t_activo,
        "pasivo": t_pasivo,
        "perdida": t_perdida,
        "ganancia": t_ganancia,
        "saldo": 0,
        "_es_total": True,
    }])

    # Fila de totales finales (debe cuadrar)
    fila_total = pd.DataFrame([{
        "n": len(grouped) + 3,
        "cuenta": "",
        "nombre_cuenta": "Total",
        "debe": t_debe,
        "haber": t_haber,
        "saldo_deudor": grouped["saldo_deudor"].sum(),
        "saldo_acreedor": grouped["saldo_acreedor"].sum(),
        "activo": t_activo + res_activo,
        "pasivo": t_pasivo + res_pasivo,
        "perdida": t_perdida + res_perdida,
        "ganancia": t_ganancia + res_ganancia,
        "saldo": 0,
        "_es_total": True,
    }])

    grouped["_es_total"] = False
    resultado_df = pd.concat([grouped, fila_sumas, fila_resultado, fila_total], ignore_index=True)
    resultado_df["n"] = range(1, len(resultado_df) + 1)

    return resultado_df[["n", "cuenta", "nombre_cuenta", "debe", "haber", "saldo_deudor", "saldo_acreedor", "activo", "pasivo", "perdida", "ganancia", "saldo", "_es_total"]]
