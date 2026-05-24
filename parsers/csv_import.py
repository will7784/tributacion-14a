import pandas as pd
from io import BytesIO


def parse_csv(file_bytes: bytes) -> pd.DataFrame:
    """
    Intenta leer CSV con separador ; y encoding latin-1 (común en ERP chilenos),
    fallback a coma y utf-8.
    """
    for sep, enc in [(";", "latin-1"), (",", "utf-8"), (";", "utf-8")]:
        try:
            df = pd.read_csv(BytesIO(file_bytes), sep=sep, encoding=enc, dtype=str)
            if len(df.columns) > 1:
                return df
        except Exception:
            continue
    raise ValueError("No se pudo leer el CSV. Verifica el formato.")
