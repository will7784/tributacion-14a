import pandas as pd
import re
from app.core.utils import normalizar_texto

def read_softland_excel(file_path):
    """
    Lee un archivo Excel de Softland y mapea sus columnas al estándar del sistema.
    """
    # Cargar Excel (Hoja1 por defecto en Softland según vimos)
    df = pd.read_excel(file_path, sheet_name=0, header=0)
    
    # Normalizar nombres de columnas originales para facilitar el mapeo
    orig_cols = {normalizar_texto(c): c for c in df.columns}
    
    # Mapeo de columnas Softland -> Estándar Ledger
    # Softland detectado: 'Cuenta', 'cpbfec', 'CodAux', 'nomaux', 'NumDoc', 'MovGlosa', 'movdebe', 'movhaber', 'saldo'
    
    # 1. Separar Cuenta en código y nombre
    def split_cuenta(val):
        if pd.isna(val): return "", ""
        parts = str(val).split(maxsplit=1)
        if len(parts) == 2:
            return parts[0], parts[1]
        return parts[0], ""

    df[['cuenta_std', 'nombre_cuenta_std']] = df['Cuenta'].apply(lambda x: pd.Series(split_cuenta(x)))

    # 2. Construir DataFrame final con nombres estándar
    std_df = pd.DataFrame()
    std_df['fecha'] = df['cpbfec']
    std_df['cuenta'] = df['cuenta_std']
    std_df['nombre_cuenta'] = df['nombre_cuenta_std']
    std_df['ficha'] = df.get('CodAux', df.get('Código Auxiliar', ''))
    std_df['razon_social'] = df.get('nomaux', df.get('Descripción Auxiliar', ''))
    
    # Documento: Tipo + Número si existen
    doc_cols = ['TtdCod', 'Tipo de Documento']
    num_cols = ['NumDoc', 'Nro. de Documento']
    
    ttd = next((df[c] for c in doc_cols if c in df.columns), pd.Series([""] * len(df)))
    num = next((df[c] for c in num_cols if c in df.columns), pd.Series([""] * len(df)))
    std_df['documento'] = ttd.astype(str) + " #" + num.astype(str)
    
    # Documento Referencia (Softland: MovTipDocRef + MovNumDocRef)
    ref_tipo = df.get('MovTipDocRef', pd.Series([""] * len(df)))
    ref_num = df.get('MovNumDocRef', pd.Series([""] * len(df)))
    std_df['doc_referencia'] = ref_tipo.astype(str) + " #" + ref_num.astype(str)
    
    std_df['vencimiento'] = df.get('Vencimiento', df['cpbfec']) # Fallback a fecha si no hay vcto
    std_df['concepto'] = df.get('MovGlosa', df.get('Glosa', ''))
    
    # Combinar Tipo + Número (e338)
    pctipo = df.get('PCTIPO', pd.Series([''] * len(df))).astype(str).str[0].str.lower()
    cpbnum = df.get('CpbNum', pd.Series([''] * len(df))).astype(str)
    std_df['comprobante'] = pctipo + cpbnum
    
    std_df['debe'] = df.get('movdebe', df.get('Debe', 0))
    std_df['haber'] = df.get('movhaber', df.get('Haber', 0))
    std_df['saldo'] = df.get('saldo', df.get('Saldo', 0))

    # Limpiar horas de las fechas y asegurar formato ISO para SQLite
    std_df['fecha'] = pd.to_datetime(std_df['fecha'], errors='coerce').dt.strftime('%Y-%m-%d')
    std_df['vencimiento'] = pd.to_datetime(std_df['vencimiento'], errors='coerce').dt.strftime('%Y-%m-%d')

    # Limpieza de nombres de columnas
    std_df.columns = [normalizar_texto(c) for c in std_df.columns]
    
    return std_df
