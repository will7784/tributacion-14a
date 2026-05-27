# Despliegue en Railway

## 1. Crear proyecto en Railway

1. Ve a [railway.app](https://railway.app) e inicia sesión.
2. Crea un **New Project** → **Deploy from GitHub repo**.
3. Selecciona `will7784/tributacion-14a`.

## 2. Configurar Variables de Entorno

En el dashboard de Railway, ve a tu servicio → **Variables** y agrega:

| Variable | Valor (ejemplo) | Descripción |
|----------|----------------|-------------|
| `SECRET_KEY` | `${{ secret() }}` | Deja la función `secret()` de Railway para generar uno automático |
| `DATA_DIR` | `/data` | Directorio donde se guardarán las bases de datos SQLite |
| `DEBUG` | `False` | Modo producción (sin recarga automática) |
| `ADMIN_USERNAME` | `will` | Tu usuario administrador (cámbialo si quieres) |
| `ADMIN_PASSWORD` | `anwi7784` | **Tu contraseña de admin** (cámbiala por algo seguro) |

> **Importante:** `ADMIN_PASSWORD` es obligatoria. Si no la configuras, la app no creará ningún usuario y no podrás iniciar sesión.

## 3. Agregar Volumen Persistente (1 GB)

Las bases de datos SQLite se guardan en archivos locales. En Railway el filesystem es efímero, así que necesitas un **volumen** para que los datos no se borren en cada redeploy.

### Opción A: Desde el dashboard web

1. En el canvas de Railway, haz **clic en el bloque de tu servicio** (`tributacion-14a`).
2. En el panel derecho busca la sección **Volumes**.
   - Si no la ves, prueba hacer **clic derecho** sobre el servicio en el canvas.
3. Clic en **New Volume**.
4. Completa:
   - **Mount Path**: `/data` (debe coincidir exactamente con `DATA_DIR`)
   - **Size**: `1` y selecciona la unidad `GB`
5. Guarda.

### Opción B: Desde la terminal (Railway CLI)

Si no ves la opción en la web, instala el CLI y ejecuta:

```bash
# Login (solo la primera vez)
railway login

# Linka tu proyecto
railway link

# Crea el volumen de 1 GB en /data
railway volume create -s tributacion-14a -m /data
```

Esto asegura que `auth.db` y los archivos `{RUT}_14a.db` persistan entre redeploys.

## 4. Deploy

Railway detectará automáticamente:
- `runtime.txt` → Python 3.12
- `requirements.txt` → dependencias
- `Procfile` / `railway.toml` → comando de inicio con Gunicorn

Solo haz push a `main` y Railway hará el deploy automáticamente.

## 5. Primer acceso

Una vez que la app esté online:

1. Ve a `https://<tu-app>.up.railway.app/login`
2. Ingresa con las credenciales que configuraste en `ADMIN_USERNAME` y `ADMIN_PASSWORD`.

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
