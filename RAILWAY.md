# Despliegue en Railway

## 1. Crear proyecto en Railway

1. Ve a [railway.app](https://railway.app) e inicia sesión.
2. Crea un **New Project** → **Deploy from GitHub repo**.
3. Selecciona `will7784/tributacion-14a`.

## 2. Configurar Variables de Entorno

En el dashboard de Railway, ve a tu servicio → **Variables** y agrega:

| Variable | Valor (ejemplo) | Descripción |
|----------|----------------|-------------|
| `SECRET_KEY` | `cambia-esto-por-un-string-largo-y-aleatorio` | Clave para firmar sesiones de Flask |
| `DATA_DIR` | `/data` | Directorio donde se guardarán las bases de datos SQLite |
| `DEBUG` | `False` | Modo producción (sin recarga automática) |

> **Importante:** Genera un `SECRET_KEY` seguro con:  
> `python -c "import secrets; print(secrets.token_urlsafe(32))"`

## 3. Agregar Volumen Persistente

Las bases de datos SQLite se guardan en archivos locales. En Railway el filesystem es efímero, así que necesitas un **volumen**:

1. En el dashboard de Railway, ve a tu servicio → **Volumes**.
2. Clic en **New Volume**.
3. Mount path: `/data`
4. Tamaño: 1 GB (puedes aumentar luego).

Esto asegura que `auth.db` y los archivos `{RUT}_14a.db` persistan entre redeploys.

## 4. Deploy

Railway detectará automáticamente:
- `runtime.txt` → Python 3.12
- `requirements.txt` → dependencias
- `Procfile` / `railway.toml` → comando de inicio con Gunicorn

Solo haz push a `main` y Railway hará el deploy automáticamente.

## 5. Primer acceso (Setup inicial)

La primera vez que entres a la app:

1. Ve a `https://<tu-app>.up.railway.app/setup`
2. Crea tu cuenta de administrador.
3. Después de eso, el setup se desactiva automáticamente y puedes usar `/login` normalmente.

> Si ya tienes un `auth.db` local con usuarios, súbelo manualmente al volumen `/data` o recréalos desde cero.

## 6. Importar datos de prueba (opcional)

Si quieres migrar una base de datos local a Railway:

1. Localmente, copia tu archivo `799171805_14a.db` (u otro).
2. En Railway, usa **Shell** del servicio para subir archivos, o conecta por CLI:
   ```bash
   railway connect
   # copia el archivo al volumen /data
   ```

## Notas

- **No subas `*.db` a Git.** Ya están en `.gitignore`.
- Si cambias `DATA_DIR`, asegúrate de que coincida con el mount path del volumen.
- En desarrollo local no necesitas configurar nada; usa los valores por defecto.
