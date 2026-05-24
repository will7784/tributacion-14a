import unicodedata
import re

def normalizar_texto(txt):
    """
    Limpia y normaliza un texto: minúsculas, sin acentos, sin caracteres especiales.
    """
    if not isinstance(txt, str):
        return str(txt)
        
    # pasar a minúscula
    txt = txt.lower()

    # eliminar acentos
    txt = ''.join(
        c for c in unicodedata.normalize('NFKD', txt)
        if not unicodedata.combining(c)
    )

    # reemplazar signos no válidos por _
    txt = re.sub(r'[^a-z0-9]+', '_', txt)

    # evitar múltiples ___ y remover _ al inicio o final
    txt = re.sub(r'_+', '_', txt).strip('_')

    return txt
