import pandas as pd
from io import BytesIO


def parse_excel(file_bytes: bytes) -> pd.DataFrame:
    """
    Lee Excel .xlsx o .xls, retorna el primer sheet.
    """
    try:
        df = pd.read_excel(BytesIO(file_bytes), engine="calamine", dtype=str)
        return df
    except Exception:
        # Fallback a openpyxl
        df = pd.read_excel(BytesIO(file_bytes), engine="openpyxl", dtype=str)
        return df
