import os
import pandas as pd
from pathlib import Path
from difflib import SequenceMatcher

DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
PLAN_BASE_PATH = DATA_DIR / "plan_cuentas_base.csv"


def cargar_plan_base() -> pd.DataFrame:
    """Carga el plan de cuentas base (KAME ONE) desde CSV."""
    # Si no existe en DATA_DIR, copiar desde el repo (Railway deploy)
    if not PLAN_BASE_PATH.exists():
        repo_path = Path("plan_cuentas_base.csv")
        if repo_path.exists():
            import shutil
            shutil.copy(repo_path, PLAN_BASE_PATH)
    if not PLAN_BASE_PATH.exists():
        return pd.DataFrame(columns=["Cuenta", "Nombre", "Ficha", "Docum.", "Un.Neg.", "Concil.", "Estado", "cuenta_sii", "cod_f22"])
    df = pd.read_csv(PLAN_BASE_PATH, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    # Asegurar que exista la columna cod_f22
    if "cod_f22" not in df.columns:
        df["cod_f22"] = ""
    return df


def guardar_plan_base(df: pd.DataFrame) -> None:
    """Guarda el plan de cuentas base en CSV."""
    df.to_csv(PLAN_BASE_PATH, index=False)


def cargar_plan_sii() -> pd.DataFrame:
    path = Path("data/plan_cuentas_sii.csv")
    if not path.exists():
        return pd.DataFrame(columns=["cuenta_sii", "nombre", "seccion", "tipo"])
    df = pd.read_csv(path, dtype=str)
    return df


def cargar_codigos_f22() -> pd.DataFrame:
    path = Path("data/codigos_f22.csv")
    if not path.exists():
        return pd.DataFrame(columns=["codigo", "nombre"])
    df = pd.read_csv(path, dtype=str)
    return df


def similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def inferir_tipo(cuenta: str) -> str:
    """Infiere tipo contable desde el primer dígito de la cuenta."""
    cuenta = str(cuenta).strip()
    if cuenta.startswith("1"):
        return "ACTIVO"
    elif cuenta.startswith("2"):
        return "PASIVO"
    elif cuenta.startswith("3"):
        return "INGRESO"
    elif cuenta.startswith("4"):
        return "EGRESO"
    return "OTRO"


def buscar_cuentas_base(query: str, plan_base: pd.DataFrame = None, limit: int = 10) -> list:
    """
    Búsqueda inteligente por nombre o código en el plan base.
    Retorna lista de dicts: {cuenta, nombre, score}
    """
    if plan_base is None:
        plan_base = cargar_plan_base()
    if plan_base.empty or not query:
        return []

    query_norm = query.lower().strip()
    resultados = []

    for _, row in plan_base.iterrows():
        cuenta = str(row.get("Cuenta", "")).strip()
        nombre = str(row.get("Nombre", "")).strip()

        score = 0.0
        if query_norm == cuenta.lower():
            score = 1.0
        elif query_norm in cuenta.lower():
            score = 0.9
        elif query_norm in nombre.lower():
            score = 0.7
        else:
            score = similar(query_norm, nombre)

        if score > 0.3:
            resultados.append({"cuenta": cuenta, "nombre": nombre, "score": score})

    resultados.sort(key=lambda x: x["score"], reverse=True)
    return resultados[:limit]


def homologar_plan(df_importado: pd.DataFrame, plan_base: pd.DataFrame = None) -> pd.DataFrame:
    """
    Recibe un DataFrame con al menos las columnas: cuenta, nombre (del cliente).
    Retorna DataFrame enriquecido con: cuenta_local, nombre_local, cuenta_base.
    """
    if plan_base is None:
        plan_base = cargar_plan_base()

    df = df_importado.copy()
    col_map = {}
    for c in df.columns:
        cl = c.lower().strip()
        if cl in ("cuenta", "codigo", "code", "id cuenta"):
            col_map["cuenta"] = c
        elif cl in ("nombre", "descripcion", "glosa", "name"):
            col_map["nombre"] = c

    cuenta_col = col_map.get("cuenta", df.columns[0])
    nombre_col = col_map.get("nombre", df.columns[1] if len(df.columns) > 1 else df.columns[0])

    df["cuenta_local"] = df[cuenta_col].astype(str).str.strip()
    df["nombre_local"] = df[nombre_col].astype(str).str.strip()
    df["cuenta_base"] = ""

    base_cuentas = plan_base.set_index("Cuenta")["Nombre"].to_dict()
    base_nombres = plan_base.set_index("Nombre")["Cuenta"].to_dict()

    # 1) Match exacto por código de cuenta
    for idx, row in df.iterrows():
        cl = row["cuenta_local"]
        if cl in base_cuentas:
            df.at[idx, "cuenta_base"] = cl

    # 2) Fuzzy match por nombre para los no matcheados
    for idx, row in df.iterrows():
        if df.at[idx, "cuenta_base"] != "":
            continue
        best_score = 0.0
        best_cuenta = ""
        nl = row["nombre_local"]
        for bn, bc in base_nombres.items():
            score = similar(nl, bn)
            if score > best_score and score > 0.75:
                best_score = score
                best_cuenta = bc
        if best_cuenta:
            df.at[idx, "cuenta_base"] = best_cuenta

    return df[["cuenta_local", "nombre_local", "cuenta_base"]]
