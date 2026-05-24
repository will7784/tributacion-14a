import pandas as pd
import xlsxwriter
import os
from datetime import datetime
from pathlib import Path
from app.core.db import run_query

def get_export_path(db_name, filename):
    """Determina la carpeta (Kame/Softland) y asegura que exista."""
    erp = "SOFTLAND" if "_softland" in db_name.lower() else "Kame"
    if not os.path.exists(erp):
        os.makedirs(erp)
    return os.path.join(erp, filename)

def export_report_to_excel(db_name, texto_busqueda, tipo_analisis="inteligente"):
    """
    Genera un archivo Excel con detalle y pivot.
    tipo_analisis: 'inteligente' (pendientes), 'historico' (todo c/status), 'comprobante' (asiento full)
    """
    # Separar términos por coma y limpiar espacios
    terminos = [t.strip() for t in texto_busqueda.split(",") if t.strip()]
    
    if not terminos:
        return None

    params = []
    
    if tipo_analisis == "comprobante":
        # Reporte de Comprobante: Busca coincidencia exacta o parecida en el ID de comprobante
        # Muestra TODO el asiento (sin filtrar cuenta 1 o 2)
        filtros = [f"comprobante LIKE ?" for _ in terminos]
        for t in terminos: params.append(f"%{t}") # Buscamos que termine o sea el comprobante
        where_clause = " OR ".join(filtros)
        
        query = f"""
        SELECT 
            fecha, comprobante, cuenta, nombre_cuenta, ficha, razon_social, documento, vencimiento, concepto,
            debe, haber, (debe - haber) AS saldo,
            COALESCE(proyecto, '') as proyecto
        FROM ledger
        WHERE ({where_clause})
        ORDER BY comprobante, cuenta;
        """
    elif tipo_analisis == "inteligente":
        # Análisis de cuentas corrientes (pendientes)
        # Agrupamos por Ficha + Documento para saber qué tiene saldo vivo,
        # pero mostramos el detalle de los movimientos que componen ese saldo.
        filtros = ["(UPPER(l.nombre_cuenta) LIKE ? OR UPPER(l.razon_social) LIKE ? OR UPPER(l.ficha) LIKE ? OR UPPER(l.concepto) LIKE ?)" for _ in terminos]
        for t in terminos:
            t_upper = f"%{t.upper()}%"
            params.extend([t_upper, t_upper, t_upper, t_upper])
        where_clause = " OR ".join(filtros)
        
        query = f"""
        WITH saldos_vivos AS (
            SELECT cuenta, ficha, documento
            FROM ledger
            GROUP BY cuenta, ficha, documento
            HAVING ABS(SUM(debe - haber)) > 0.1
        )
        SELECT 
            l.fecha, l.comprobante, l.cuenta, l.nombre_cuenta, l.ficha, l.razon_social, l.documento, l.vencimiento,
            (l.debe - l.haber) AS saldo,
            COALESCE(l.proyecto, '') as proyecto
        FROM ledger l
        INNER JOIN saldos_vivos sv 
            ON l.cuenta = sv.cuenta 
           AND COALESCE(l.ficha, '') = COALESCE(sv.ficha, '')
           AND COALESCE(l.documento, '') = COALESCE(sv.documento, '')
        WHERE ({where_clause})
          AND substr(l.cuenta, 1, 1) IN ('1','2','3','4')
        ORDER BY l.fecha, l.comprobante, l.cuenta, l.razon_social;
        """
    else:
        # Histórico Completo (con columna de pendiente)
        filtros = ["(UPPER(l.nombre_cuenta) LIKE ? OR UPPER(l.razon_social) LIKE ? OR UPPER(l.ficha) LIKE ? OR UPPER(l.concepto) LIKE ?)" for _ in terminos]
        for t in terminos:
            t_upper = f"%{t.upper()}%"
            params.extend([t_upper, t_upper, t_upper, t_upper])
        where_clause_hist = " OR ".join(filtros)
        
        query = f"""
        WITH saldos_doc AS (
            SELECT cuenta, ficha, documento, SUM(debe - haber) as saldo_neto
            FROM ledger
            GROUP BY cuenta, ficha, documento
        )
        SELECT 
            l.fecha, l.comprobante, l.cuenta, l.nombre_cuenta, l.ficha, l.razon_social, l.documento, l.vencimiento,
            (l.debe - l.haber) AS saldo,
            CASE WHEN ABS(s.saldo_neto) > 0.1 THEN 'si' ELSE 'no' END as pendiente,
            COALESCE(l.proyecto, '') as proyecto
        FROM ledger l
        JOIN saldos_doc s 
            ON l.cuenta = s.cuenta 
           AND COALESCE(l.ficha, '') = COALESCE(s.ficha, '') 
           AND COALESCE(l.documento, '') = COALESCE(s.documento, '')
        WHERE ({where_clause_hist})
          AND substr(l.cuenta, 1, 1) IN ('1','2','3','4')
        ORDER BY l.fecha, l.comprobante, l.cuenta, l.razon_social;
        """
    
    df = run_query(query, db_name, params=params)
    
    if df.empty:
        return None
        
    from pathlib import Path
    db_id = Path(db_name).stem
    fecha_str = datetime.now().strftime("%d%m%Y")
    nombre_base = f"analisis_{texto_busqueda.lower().replace(',','_')[:30]}_{tipo_analisis}_{fecha_str}_{db_id}.xlsx"
    nombre_archivo = get_export_path(db_name, nombre_base)
    
    # Crear pivot
    df["cuenta_nombre"] = df["cuenta"] + " " + df["nombre_cuenta"]
    pivot = df.pivot_table(
        index="razon_social",
        columns="cuenta_nombre",
        values="saldo",
        aggfunc="sum",
        fill_value=0,
        margins=False 
    ).reset_index()

    # Agregar columna TOTAL horizontal en la Pivot
    cols_cuentas = pivot.columns[1:] # Saltar 'razon_social'
    pivot['TOTAL_REGISTRO'] = pivot[cols_cuentas].sum(axis=1)

    # Leer Metadata de la Empresa y Período
    empresa_nombre, empresa_rut = "No definida", ""
    fecha_min = "2025-01-01"
    try:
        meta_df = run_query("SELECT empresa_nombre, empresa_rut FROM metadata LIMIT 1", db_name)
        if not meta_df.empty:
            empresa_nombre = meta_df.iloc[0]['empresa_nombre']
            empresa_rut = meta_df.iloc[0]['empresa_rut']
        
        # Buscar fecha mínima para el período
        f_min_df = run_query("SELECT MIN(fecha) as min_f FROM ledger", db_name)
        if not f_min_df.empty and f_min_df.iloc[0]['min_f']:
            fecha_min = f_min_df.iloc[0]['min_f']
            # Convertir de ISO a DD-MM-YYYY si es necesario
            if "-" in fecha_min:
                parts = fecha_min.split("-")
                fecha_min = f"{parts[2]}/{parts[1]}/{parts[0]}"
    except:
        pass

    with pd.ExcelWriter(nombre_archivo, engine="xlsxwriter") as writer:
        # Asegurar que las fechas salgan homogéneas DD/MM/YYYY.
        for col in ["fecha", "vencimiento"]:
            if col in df.columns:
                raw_vals = df[col].astype(str)
                dt_iso = pd.to_datetime(df[col], format="%Y-%m-%d", errors="coerce")
                dt_dayfirst = pd.to_datetime(df[col], errors="coerce", dayfirst=True)
                dt_vals = dt_iso.fillna(dt_dayfirst)
                df[col] = dt_vals.dt.strftime("%d/%m/%Y")
                df.loc[dt_vals.isna(), col] = raw_vals[dt_vals.isna()]
                iso_mask = df[col].astype(str).str.match(r"^\d{4}-\d{2}-\d{2}$", na=False)
                if iso_mask.any():
                    parts = df.loc[iso_mask, col].astype(str).str.split("-", expand=True)
                    df.loc[iso_mask, col] = parts[2] + "/" + parts[1] + "/" + parts[0]

        df_detalle = df.drop(columns=["cuenta_nombre"])
        # Escribimos los datos base sin encabezados (los pondremos nosotros luego)
        df_detalle.to_excel(writer, sheet_name="detalle", index=False, startrow=6, header=False)
        pivot.to_excel(writer, sheet_name="pivot", index=False, startrow=6, header=False)
        
        workbook = writer.book
        ws_det = writer.sheets["detalle"]
        ws_piv = writer.sheets["pivot"]
        
        # Eliminar las hojas que pandas crea por defecto con encabezados
        # para escribirlos nosotros manualmente con formato
        
        # Formatos de Encabezado Corporativo
        f_corp = workbook.add_format({"bold": True, "font_size": 14, "align": "center"})
        f_info = workbook.add_format({"bold": True, "font_size": 10, "align": "center"})
        
        # Escribir Cabecera en ambas hojas
        for ws in [ws_det, ws_piv]:
            num_cols = len(df_detalle.columns) if ws == ws_det else len(pivot.columns)
            col_letter = xlsxwriter.utility.xl_col_to_name(num_cols - 1)
            ws.merge_range(f"A1:{col_letter}1", f"REPORTE {tipo_analisis.upper()} CONTABLE", f_corp)
            ws.merge_range(f"A2:{col_letter}2", f"EMPRESA: {empresa_nombre}", f_info)
            ws.merge_range(f"A3:{col_letter}3", f"RUT: {empresa_rut}", f_info)
            ws.merge_range(f"A4:{col_letter}4", f"PERÍODO: {fecha_min} HASTA {datetime.now().strftime('%d/%m/%Y')}", f_info)
            ws.merge_range(f"A5:{col_letter}5", f"FECHA EMISIÓN: {datetime.now().strftime('%d-%m-%Y')}", f_info)

        # Formatos
        f_header = workbook.add_format({"bold": True, "bg_color": "#2C3E50", "font_color": "white", "border": 1})
        f_num = workbook.add_format({"num_format": "#,##0;[Red](#,##0)", "border": 1})
        
        # Aplicar formato número a las columnas de dinero en detalle (no es necesario si escribimos todo abajo)
        f_text = workbook.add_format({"border": 1})
        f_total = workbook.add_format({"bold": True, "num_format": "#,##0;[Red](#,##0)", "border": 1, "bg_color": "#F2F2F2"})
        f_total_text = workbook.add_format({"bold": True, "border": 1, "bg_color": "#F2F2F2"})

        # --- Inmovilizar Paneles ---
        ws_det.freeze_panes(6, 4)  # E7
        ws_piv.freeze_panes(6, 1)  # B7

        # --- DETALLE ---
        num_rows_det = len(df_detalle)
        num_cols_det = len(df_detalle.columns)
        header_row = 5
        data_start_row = 6
        
        # Autofiltro: abarca desde los encabezados (fila 5) hasta el final de los datos
        ws_det.autofilter(header_row, 0, header_row + num_rows_det, num_cols_det - 1)

        for col_num, value in enumerate(df_detalle.columns):
            ws_det.write(header_row, col_num, value, f_header)
            ws_det.set_column(col_num, col_num, 18)
            
            # Formato numérico para dinero, texto para el resto
            is_money = value.lower() in ["debe", "haber", "saldo"]
            fmt = f_num if is_money else f_text
            
            for row_num_in_df in range(num_rows_det):
                val = df_detalle.iloc[row_num_in_df, col_num]
                write_row = data_start_row + row_num_in_df
                # Forzar que la fecha se escriba si existe
                if pd.isna(val) or val == "":
                    ws_det.write_blank(write_row, col_num, None, fmt)
                else:
                    ws_det.write(write_row, col_num, val, fmt)

        # Totales con SUBTOTAL
        total_row = data_start_row + num_rows_det
        ws_det.write(total_row, 0, "TOTAL GENERAL", f_total_text)
        for c in range(1, num_cols_det):
            col_name = df_detalle.columns[c].lower()
            if col_name in ["debe", "haber", "saldo"]:
                col_letter = xlsxwriter.utility.xl_col_to_name(c)
                # Ojo: SUBTOTAL usa el rango de datos desde data_start_row+1 hasta total_row
                ws_det.write_formula(total_row, c, f"=SUBTOTAL(9, {col_letter}{data_start_row+1}:{col_letter}{total_row})", f_total)
            else:
                ws_det.write_blank(total_row, c, None, f_total_text)

        # --- PIVOT ---
        num_rows_piv = len(pivot)
        num_cols_piv = len(pivot.columns)
        header_row_piv = 5
        data_start_piv = 6
        
        # Autofiltro en la Pivot
        ws_piv.autofilter(header_row_piv, 0, header_row_piv + num_rows_piv, num_cols_piv - 1)

        for col_num, value in enumerate(pivot.columns):
            ws_piv.write(header_row_piv, col_num, value, f_header)
            ws_piv.set_column(col_num, col_num, 20)
            
            fmt = f_num if col_num > 0 else f_text
            for row_num_in_piv in range(num_rows_piv):
                val = pivot.iloc[row_num_in_piv, col_num]
                write_row = data_start_piv + row_num_in_piv
                if pd.isna(val):
                    ws_piv.write_blank(write_row, col_num, None, fmt)
                else:
                    ws_piv.write(write_row, col_num, val, fmt)

        # Fila de TOTAL con SUBTOTAL en Pivot
        total_row_piv = data_start_piv + num_rows_piv
        ws_piv.write(total_row_piv, 0, "TOTAL GENERAL", f_total_text)
        for c in range(1, num_cols_piv):
            col_letter = xlsxwriter.utility.xl_col_to_name(c)
            ws_piv.write_formula(total_row_piv, c, f"=SUBTOTAL(9, {col_letter}{data_start_piv+1}:{col_letter}{total_row_piv})", f_total)

    return nombre_archivo

def export_balance_8_columnas(db_name, fecha_tope):
    """
    Genera un Balance Tributario de 8 Columnas hasta la fecha especificada.
    """
    query = """
    SELECT 
        cuenta, 
        nombre_cuenta, 
        SUM(debe) as debe, 
        SUM(haber) as haber
    FROM ledger
    WHERE fecha <= ?
    GROUP BY cuenta, nombre_cuenta
    ORDER BY cuenta
    """
    df = run_query(query, db_name, params=[fecha_tope])
    
    if df.empty:
        return None

    # Cálculos de las 8 columnas
    df['saldo_deudor'] = df.apply(lambda x: max(0, x['debe'] - x['haber']), axis=1)
    df['saldo_acreedor'] = df.apply(lambda x: max(0, x['haber'] - x['debe']), axis=1)
    
    # Clasificación (Lógica 1-2 Balance, 3-4 Resultados)
    # Col 1: Activo, Col 2: Pasivo, Col 3: Pérdida, Col 4: Ganancia
    def clasificar(row):
        digit = str(row['cuenta'])[0]
        net = row['debe'] - row['haber']
        activo = pasivo = perdida = ganancia = 0
        
        if digit in ['1', '2']:
            if net >= 0:
                activo = net
            else:
                pasivo = abs(net)
        else: # 3 o 4
            if net >= 0:
                perdida = net
            else:
                ganancia = abs(net)
        return pd.Series([activo, pasivo, perdida, ganancia])

    df[['activo', 'pasivo', 'perdida', 'ganancia']] = df.apply(clasificar, axis=1)

    from pathlib import Path
    db_id = Path(db_name).stem
    f_str = datetime.now().strftime("%d%m%Y")
    nombre_base = f"balance_8_columnas_{fecha_tope.replace('-','')}_{f_str}_{db_id}.xlsx"
    nombre_archivo = get_export_path(db_name, nombre_base)

    # Leer Metadata de la Empresa y Período
    empresa_nombre, empresa_rut = "No definida", ""
    fecha_min = "01/01/2025"
    try:
        meta_df = run_query("SELECT empresa_nombre, empresa_rut FROM metadata LIMIT 1", db_name)
        if not meta_df.empty:
            empresa_nombre = meta_df.iloc[0]['empresa_nombre']
            empresa_rut = meta_df.iloc[0]['empresa_rut']
        
        f_min_df = run_query("SELECT MIN(fecha) as min_f FROM ledger", db_name)
        if not f_min_df.empty and f_min_df.iloc[0]['min_f']:
            # Viene en YYYY-MM-DD
            y, m, d = f_min_df.iloc[0]['min_f'].split("-")
            fecha_min = f"{d}/{m}/{y}"
    except:
        pass

    # Formatear fecha tope
    f_tope_fmt = fecha_tope
    if "-" in fecha_tope:
        y, m, d = fecha_tope.split("-")
        f_tope_fmt = f"{d}-{m}-{y}"

    with pd.ExcelWriter(nombre_archivo, engine="xlsxwriter") as writer:
        df.to_excel(writer, sheet_name="Balance", index=False, startrow=5)
        workbook = writer.book
        ws = writer.sheets["Balance"]
        
        # Formatos de Encabezado Corporativo
        f_corp = workbook.add_format({"bold": True, "font_size": 16, "align": "center", "bg_color": "#F4F6F7"})
        f_info = workbook.add_format({"bold": True, "font_size": 11, "align": "left"})
        
        # Escribir Cabecera
        ws.merge_range("A1:J1", "BALANCE GENERAL TRIBUTARIO (8 COLUMNAS)", f_corp)
        ws.write("A2", f"EMPRESA: {empresa_nombre}", f_info)
        ws.write("A3", f"RUT: {empresa_rut}", f_info)
        ws.write("A4", f"PERÍODO HASTA: {f_tope_fmt}", f_info)
        ws.write("A5", f"FECHA EMISIÓN: {datetime.now().strftime('%d-%m-%Y')}", f_info)

        # Formatos
        f_header = workbook.add_format({"bold": True, "bg_color": "#2C3E50", "font_color": "white", "border": 1, "align": "center"})
        f_num = workbook.add_format({"num_format": '"$" #,##0;[Red]"$" (#,##0)', "border": 1})
        f_border = workbook.add_format({"border": 1})
        f_total = workbook.add_format({"bold": True, "num_format": '"$" #,##0;[Red]"$" (#,##0)', "border": 1, "bg_color": "#2C3E50", "font_color": "white"})
        f_res = workbook.add_format({"bold": True, "num_format": '"$" #,##0;[Red]"$" (#,##0)', "border": 1, "bg_color": "#2C3E50", "font_color": "white"})
        f_final = workbook.add_format({"bold": True, "num_format": '"$" #,##0;[Red]"$" (#,##0)', "border": 1, "bg_color": "#2C3E50", "font_color": "white"})

        # Encabezados (ajustados a fila 6)
        headers = ["Cuenta", "Descripción", "Debe", "Haber", "S. Deudor", "S. Acreedor", "Activo", "Pasivo", "Pérdida", "Ganancia"]
        for col_num, name in enumerate(headers):
            ws.write(5, col_num, name, f_header)
            # Ajuste de columnas: Cuenta (0) más estrecha, Descripción (1) más ancha, resto 15
            if col_num == 0: ws.set_column(col_num, col_num, 15)
            elif col_num == 1: ws.set_column(col_num, col_num, 45)
            else: ws.set_column(col_num, col_num, 15)

        # Aplicar formato numérico y bordes a las filas de datos
        num_rows = len(df)
        for r in range(num_rows):
            ws.write(r + 6, 0, df.iloc[r, 0], f_border) # Cuenta con borde
            ws.write(r + 6, 1, df.iloc[r, 1], f_border) # Descripción con borde
            for c in range(2, 10): # Columnas numéricas
                ws.write(r + 6, c, df.iloc[r, c], f_num)

        # Totales (ajustados por startrow=5)
        offset = 6
        total_row = num_rows + offset
        ws.write(total_row, 0, "SUMAS TOTALES", f_total)
        ws.write_blank(total_row, 1, None, f_total) # Rellenar descripción en fila total
        for c in range(2, 10):
            col_letter = xlsxwriter.utility.xl_col_to_name(c)
            ws.write_formula(total_row, c, f"=SUM({col_letter}7:{col_letter}{total_row})", f_total)

        # Resultado del Ejercicio (Fila de Diferencia)
        res_row = total_row + 1
        ws.write(res_row, 0, "UTILIDAD/PÉRDIDA DEL EJERCICIO", f_res)
        ws.write_blank(res_row, 1, None, f_res) # Rellenar descripción
        # Columnas Debe y Haber también pintadas
        ws.write_blank(res_row, 2, None, f_res)
        ws.write_blank(res_row, 3, None, f_res)
        ws.write_blank(res_row, 4, None, f_res)
        ws.write_blank(res_row, 5, None, f_res)
        
        # Activo vs Pasivo: La diferencia va donde falte para cuadrar
        # G (Activo) vs H (Pasivo)
        ws.write_formula(res_row, 6, f"=IF(G{total_row+1}<H{total_row+1}, H{total_row+1}-G{total_row+1}, \"\")", f_res)
        ws.write_formula(res_row, 7, f"=IF(H{total_row+1}<G{total_row+1}, G{total_row+1}-H{total_row+1}, \"\")", f_res)
        
        # Pérdida vs Ganancia: I (Pérdida) vs J (Ganancia)
        ws.write_formula(res_row, 8, f"=IF(I{total_row+1}<J{total_row+1}, J{total_row+1}-I{total_row+1}, \"\")", f_res)
        ws.write_formula(res_row, 9, f"=IF(J{total_row+1}<I{total_row+1}, I{total_row+1}-J{total_row+1}, \"\")", f_res)

        # Totales Finales (TOTALES)
        final_row = res_row + 1
        ws.write(final_row, 0, "TOTALES", f_final)
        ws.write_blank(final_row, 1, None, f_final)
        # Columnas Debe (C) y Haber (D) se mantienen
        ws.write_formula(final_row, 2, f"=C{total_row+1}", f_final)
        ws.write_formula(final_row, 3, f"=D{total_row+1}", f_final)
        # El resto suma Totales + Resultado (Usamos SUM para manejar las celdas vacías "")
        for c in range(4, 10):
            col_letter = xlsxwriter.utility.xl_col_to_name(c)
            ws.write_formula(final_row, c, f"=SUM({col_letter}{total_row+1}, {col_letter}{res_row+1})", f_final)

    return nombre_archivo

def export_libro_mayor(db_name, texto_busqueda):
    """
    Genera el Libro Mayor (General Ledger) con glosas y saldos acumulados.
    """
    params = []
    where_clause = "1=1"
    
    if texto_busqueda != "*":
        terminos = [t.strip() for t in texto_busqueda.split(",") if t.strip()]
        filtros = ["(UPPER(nombre_cuenta) LIKE ? OR UPPER(cuenta) LIKE ? OR UPPER(razon_social) LIKE ? OR UPPER(concepto) LIKE ?)" for _ in terminos]
        for t in terminos:
            t_upper = f"%{t.upper()}%"
            params.extend([t_upper, t_upper, t_upper, t_upper])
        where_clause = " OR ".join(filtros)

    query = f"""
    SELECT 
        fecha, comprobante, cuenta, nombre_cuenta, ficha, razon_social, documento, debe, haber, concepto, 
        COALESCE(proyecto, '') as proyecto
    FROM ledger
    WHERE {where_clause}
    ORDER BY cuenta, fecha, comprobante
    """
    df = run_query(query, db_name, params=params)
    
    if df.empty:
        return None

    # Formatear fechas a DD/MM/YYYY antes de procesar
    df['fecha'] = pd.to_datetime(df['fecha'], errors="coerce").dt.strftime("%d/%m/%Y").fillna("")

    from pathlib import Path
    db_id = Path(db_name).stem
    f_str = datetime.now().strftime("%d%m%Y")
    nombre_base = f"libro_mayor_{f_str}_{db_id}.xlsx"
    nombre_archivo = get_export_path(db_name, nombre_base)

    # Leer Metadata
    empresa_nombre, empresa_rut = "No definida", ""
    try:
        meta_df = run_query("SELECT empresa_nombre, empresa_rut FROM metadata LIMIT 1", db_name)
        if not meta_df.empty:
            empresa_nombre = meta_df.iloc[0]['empresa_nombre']
            empresa_rut = meta_df.iloc[0]['empresa_rut']
    except: pass

    with pd.ExcelWriter(nombre_archivo, engine="xlsxwriter") as writer:
        workbook = writer.book
        ws = workbook.add_worksheet("Libro Mayor")

        # Formatos
        f_corp = workbook.add_format({"bold": True, "font_size": 16, "align": "center"})
        f_info = workbook.add_format({"bold": True, "font_size": 11, "align": "center"})
        f_header_acc = workbook.add_format({"bold": True, "bg_color": "#FAD7A0", "border": 1}) # Naranja suave para cuenta
        f_header_table = workbook.add_format({"bold": True, "bg_color": "#2C3E50", "font_color": "white", "border": 1, "align": "center"})
        f_num = workbook.add_format({"num_format": '"$" #,##0;[Red]"$" (#,##0)', "border": 1})
        f_date = workbook.add_format({"num_format": "dd/mm/yyyy", "border": 1, "align": "center"})
        f_text = workbook.add_format({"border": 1})
        f_total_acc = workbook.add_format({"bold": True, "num_format": '"$" #,##0;[Red]"$" (#,##0)', "border": 1, "bg_color": "#D5D8DC"})

        # Cabecera Corporativa
        ws.merge_range("A1:J1", "LIBRO MAYOR CONTABLE", f_corp)
        ws.merge_range("A2:J2", f"EMPRESA: {empresa_nombre} (RUT: {empresa_rut})", f_info)
        ws.merge_range("A3:J3", f"FECHA EMISIÓN: {datetime.now().strftime('%d-%m-%Y %H:%M')}", f_info)

        # Definir Columnas
        headers = ["Comprobante", "Fecha", "Ficha/RUT", "Razón Social", "Documento", "Concepto/Glosa", "Debe", "Haber", "Saldo", "Proyecto"]
        col_widths = [12, 12, 15, 25, 18, 50, 15, 15, 15, 15]
        for i, width in enumerate(col_widths):
            ws.set_column(i, i, width)

        current_row = 4
        # Agrupar por cuenta para el reporte estructurado
        cuentas = df['cuenta'].unique()
        
        for account in cuentas:
            df_acc = df[df['cuenta'] == account]
            account_name = df_acc.iloc[0]['nombre_cuenta']
            
            # Escribir Título de Cuenta
            ws.merge_range(current_row, 0, current_row, 9, f"CUENTA: {account} {account_name}", f_header_acc)
            current_row += 1
            
            # Escribir Encabezado de Tabla por Cuenta
            for i, h in enumerate(headers):
                ws.write(current_row, i, h, f_header_table)
            current_row += 1
            
            # Movimientos
            running_balance = 0
            sub_debe = sub_haber = 0
            
            for index, row in df_acc.iterrows():
                d = float(row['debe'])
                h = float(row['haber'])
                running_balance += (d - h)
                sub_debe += d
                sub_haber += h
                
                # Escribir Fila
                ws.write(current_row, 0, row['comprobante'], f_text)
                ws.write(current_row, 1, str(row['fecha']), f_text)
                ws.write(current_row, 2, row['ficha'] or "", f_text)
                ws.write(current_row, 3, row['razon_social'] or "", f_text)
                ws.write(current_row, 4, row['documento'] or "", f_text)
                ws.write(current_row, 5, row['concepto'] or "", f_text)
                ws.write(current_row, 6, d, f_num)
                ws.write(current_row, 7, h, f_num)
                ws.write(current_row, 8, running_balance, f_num)
                ws.write(current_row, 9, row['proyecto'], f_text)
                current_row += 1
            
            # Totales de la Cuenta
            ws.write(current_row, 0, "TOTAL CUENTA", f_total_acc)
            for i in range(1, 6): ws.write_blank(current_row, i, None, f_total_acc)
            ws.write(current_row, 6, sub_debe, f_total_acc)
            ws.write(current_row, 7, sub_haber, f_total_acc)
            ws.write(current_row, 8, running_balance, f_total_acc)
            ws.write_blank(current_row, 9, None, f_total_acc)
            
            current_row += 2 # Espacio entre cuentas

    return nombre_archivo
