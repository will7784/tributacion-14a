import pandas as pd
from app.core.utils import normalizar_texto

def read_kame_excel(file_path, sheet_name=0):
    """
    Lee archivo KAME (Excel/CSV) y normaliza columnas/fechas.
    """
    path = str(file_path or "").strip()
    lower = path.lower()
    if lower.endswith(".csv"):
        last_err = None
        for enc in ("utf-8", "latin-1", "cp1252"):
            try:
                df = pd.read_csv(path, sep=";", encoding=enc, header=0, dtype=str)
                break
            except Exception as e:
                last_err = e
        else:
            raise last_err
    else:
        df = pd.read_excel(path, sheet_name=sheet_name, header=0)
    
    # Normalizar columnas
    df.columns = [normalizar_texto(c) for c in df.columns]

    # Alias defensivos para encabezados con codificación extraña.
    if "razon_social" not in df.columns:
        alt = [c for c in df.columns if ("social" in c and "raz" in c)]
        if alt:
            df = df.rename(columns={alt[0]: "razon_social"})
    
    # Combinar Tipo + Comprobante (ej: e338)
    if 'tipo' in df.columns and 'comprobante' in df.columns:
        df['comprobante'] = df['tipo'].astype(str).str[0].str.lower() + df['comprobante'].astype(str)
    
    # Campo Proyecto (KAME utiliza 'Unidad de Negocio')
    if 'unidad_de_negocio' in df.columns:
        df['proyecto'] = df['unidad_de_negocio']
    
    # Normalizar Fecha a ISO (YYYY-MM-DD) para que las queries SQL funcionen bien
    if 'fecha' in df.columns:
        fecha_dt = pd.to_datetime(df['fecha'], errors='coerce', dayfirst=True)
        if fecha_dt.isna().any():
            if 'comprobante' in df.columns:
                by_comp = fecha_dt.groupby(df['comprobante']).transform('first')
                fecha_dt = fecha_dt.fillna(by_comp)
            # En algunos Excel KAME hay filas con fecha visualmente repetida pero celda vacía.
            fecha_dt = fecha_dt.ffill()
        df['fecha'] = fecha_dt.dt.strftime('%Y-%m-%d')
    
    # Validaciones básicas de columnas requeridas
    required_cols = ['cuenta', 'nombre_cuenta', 'debe', 'haber', 'saldo']
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"Faltan columnas requeridas en archivo KAME: {missing}")
    
    # Columna de Referencia para Análisis Inteligente (KAME usualmente usa documento)
    df['doc_referencia'] = df.get('documento', pd.Series([""] * len(df))).astype(str)

    return df
