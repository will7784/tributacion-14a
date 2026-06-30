# Log de Sesión - Tributación 14A

> Fecha: 2026-05-27
> Último commit: `92afa00`

---

## 1. Deploy en Railway

### Estado
- ✅ Proyecto deployado en Railway: `tributacion-14a-production.up.railway.app`
- ✅ Volumen persistente creado en `/data` (1 GB)
- ✅ Variables de entorno configuradas:
  - `SECRET_KEY` = `${{ secret() }}`
  - `DATA_DIR` = `/data`
  - `DEBUG` = `false`
  - `ADMIN_USERNAME` = `will`
  - `ADMIN_PASSWORD` = `anwi7784`

### Problemas encontrados y soluciones
1. **Superusuario hardcodeado removido**: Se reemplazó por creación automática desde variables de entorno (`ADMIN_USERNAME`/`ADMIN_PASSWORD`).
2. **SQLite efímero**: Las bases de datos SQLite se guardan ahora en `DATA_DIR` (volumen persistente) para que no se borren en cada redeploy.
3. **Plan de Cuentas Base se perdía en cada deploy**: El CSV `plan_cuentas_base.csv` estaba en el directorio del proyecto (se sobrescribía con cada deploy). Solución: ahora se guarda en `DATA_DIR` y se copia desde el repo si el repo tiene una versión más reciente/más grande.

---

## 2. Plan de Cuentas Base

### Cambios realizados
- **Título**: "Plan Base KAME ONE" → "Plan de Cuentas Base"
- **Cuenta SII**: Solo editable para cuentas **nivel 3** (`X.XX.XX.00` donde los primeros 3 grupos no son `.00`)
- **Cód. F22**: Nueva columna, solo visible para cuentas de **resultado** nivel 3 (empiezan con `3` o `4`)
- **Búsqueda inteligente**: Ambas columnas usan Select2 (librería agregada vía CDN)
- **Filtro**: Checkbox "Solo cuentas clasificables (nivel 3)" oculta niveles 1, 2 y 4

### Clasificación automática SII
- Se clasificaron **86 de 87 cuentas nivel 3** automáticamente usando fuzzy matching + mapeos forzados.
- La única cuenta sin clasificar: `1.03.11.00` — Contratos de leasing largo plazo (neto) (score muy bajo)
- Archivo de códigos F22 creado: `data/codigos_f22.csv`

### Pendiente para mañana
- [ ] Revisar si la clasificación automática SII es correcta para todas las cuentas
- [ ] Completar columna **Cód. F22** para las cuentas de resultado (3.x y 4.x)
- [ ] Asignar manualmente `1.03.11.00` si es necesario

---

## 3. DJ 1847

### Cambios realizados
- Ahora muestra **solo cuentas nivel 4** (sin totalizadores)
- **Columnas** según formato SII:
  - N°
  - Id. Plan Cuentas (código nivel 4 del balance)
  - Id. Cuenta SII (código SII del **padre nivel 3**)
  - Nombre Cuenta
  - Débitos, Créditos, Saldo Deudor, Saldo Acreedor
  - Activo, Pasivo, Pérdidas, Ganancias
  - **Cód. F22** (solo resultado)
  - **Valor Tributario** (solo Activo/Pasivo = Activo - Pasivo)

### Lógica de inferencia automática
- Si la cuenta del balance está **homologada manualmente**, usa la homologación.
- Si **NO está homologada**, infiere el padre nivel 3 directamente desde el código de cuenta (ej. `1.01.01.04` → `1.01.01.00`).

### Pendiente para mañana
- [ ] Validar que el Cód. F22 se muestre correctamente cuando se complete en el Plan Base
- [ ] Revisar si el formato de exportación Excel cumple con lo que pide el SII
- [ ] Verificar si se necesita exportar a CSV también (formato múltiple del SII)

---

## 4. Archivos nuevos/modificados clave

| Archivo | Cambio |
|---------|--------|
| `data/codigos_f22.csv` | Nuevo: lista de códigos F22 del SII |
| `plan_cuentas_base.csv` | Movido a raíz (se copia a `DATA_DIR` en Railway) |
| `core/plan_cuentas.py` | Ahora usa `DATA_DIR`; carga/guarda `cod_f22` |
| `core/db.py` | Rutas SQLite ahora usan `DATA_DIR` |
| `core/auth.py` | `auth.db` ahora usa `DATA_DIR` |
| `core/balance.py` | Rutas de balance ahora usan `DATA_DIR` |
| `templates/plan_base.html` | Dos columnas (SII + F22) con Select2 |
| `templates/dj1847.html` | Estructura completa SII |
| `app.py` | APIs de DJ 1847, codigos_f22, debug |
| `railway.toml` | Configuración de deploy |
| `RAILWAY.md` | Instrucciones de deploy |

---

## 5. Notas técnicas importantes

- **Gunicorn**: `__main__` no se ejecuta en producción; `init_auth_db()` y `_ensure_admin()` se llaman al importar el módulo `app.py`.
- **Select2**: Se carga solo en `plan_base.html` vía CDN. DataTables puede tener conflictos con Select2 en filas ocultas; se inicializa Select2 después de crear DataTables.
- **DataTables columna oculta**: La columna "Nivel" (índice 9) se usa para filtrar cuentas nivel 3 con el checkbox.
- **Formato de cuentas**: El plan base usa formato `X.XX.XX.XX` (4 grupos). Los códigos SII también usan 4 grupos.

---

## 6. Próximos pasos sugeridos (post-sesión)

1. ✅ **Corregir orden de columnas en exportación Excel DJ1847** — Ahora usa `columns=` explícito con orden oficial SII
2. ✅ **Usar nombres oficiales del SII como headers del Excel** — `DJ1847_HEADERS` con nombres del instructivo
3. ✅ **Corregir Valor Tributario: pasivos con signo negativo** — Según instructivo SII, pasivos van negativos
4. [ ] Validar DJ 1847 con datos reales (exportar Excel y verificar)
5. [ ] Implementar exportación CSV según formato SII (Sección B + Sección C)
6. [ ] Revisar DJ 1926 para ver si necesita ajustes similares
7. [ ] Completar columna **Cód. F22** para cuentas de resultado (3.x y 4.x) en Plan Base
8. [ ] Revisar si la clasificación automática SII es correcta para todas las cuentas
9. [ ] Agregar tests si el usuario quiere robustecer la app

---

## 7. Pendiente NUEVO (2026-06-30)

### Exportación Excel DJ1847 — Columnas desordenadas

**Problema reportado**: El Excel exportado tenía las columnas en orden alfabético (`activo`, `cod_f22`, `cuenta`, `cuenta_sii`, `debe`...) en lugar del orden oficial del SII.

**Causa**: `pd.DataFrame(data.get("filas", []))` sin parámetro `columns=` → pandas ordena alfabéticamente cuando deserializa JSON.

**Solución aplicada**:
- Se agregó `DJ1847_COLUMNAS` con el orden oficial del instructivo SII, Sección C
- Se agregó `DJ1847_HEADERS` con los nombres oficiales de cada columna
- Se corrigió `api_exportar_dj1847` para usar `columns=DJ1847_COLUMNAS` y `rename(columns=DJ1847_HEADERS)`
- Se corrigió el cálculo de **Valor Tributario**: ahora los pasivos van con signo negativo según el instructivo

**Orden oficial SII DJ1847 Sección C**:
1. N°
2. Id. plan de cuentas utilizado en registros contables
3. Id. cuenta según clasificador de cuentas
4. Nombre de la Cuenta según registros contables
5. Débitos
6. Créditos
7. Saldo Deudor
8. Saldo Acreedor
9. Activo
10. Pasivo
11. Pérdidas
12. Ganancias
13. Conceptos y/o Partidas Que Componen el Resultado Financiero (Cód. F22)
14. Valor Tributario

**Estado**: ✅ Corregido en `app.py` — pendiente validación con datos reales.
