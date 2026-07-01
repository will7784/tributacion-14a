# Log de Sesión - Tributación 14A

> Fecha: 2026-06-30 (Sesión actual)
> Último commit: `8abc354`
> URL Producción: https://tributacion-14a-production.up.railway.app/

---

## Historial de Cambios (Resumen Completo)

### 1. Deploy en Railway (Sesión anterior)
- ✅ Proyecto deployado en Railway
- ✅ Volumen persistente en `/data` (1 GB)
- ✅ Variables de entorno configuradas
- ✅ Superusuario desde variables de entorno
- ✅ SQLite persistente en volumen

### 2. Plan de Cuentas Base
- ✅ Clasificación automática SII para 86/87 cuentas nivel 3
- ✅ Columna Cód. F22 agregada (visible para cuentas 3.x y 4.x)
- ✅ Select2 para búsqueda inteligente
- ✅ Filtro "Solo cuentas clasificables"
- ⚠️ Cód. F22 aún vacío para la mayoría de cuentas (pendiente asignación manual)

### 3. DJ 1847 — Progreso Extensivo

#### ✅ Completado:
- Estructura de tabla con columnas oficiales SII
- Inferencia automática de cuenta padre nivel 3
- Exportación Excel con orden oficial de columnas
- Exportación CSV con formato SII (Sección B + Sección C)
- Valor Tributario: pasivos con signo negativo según instructivo
- Cód. F22 en tabla (cuando está disponible en plan base)
- localStorage como respaldo ante pérdida de datos Railway
- Auto-carga de datos al abrir la página
- Manejo de arrays vacíos en "Guardar Cambios"
- localStorage key unificada con año comercial

#### ⚠️ Problemas Activos (CRÍTICO):

**PROBLEMA #1: Datos Sección B no persisten visualmente**
- Código de actividad económica, folios inicio/fin no aparecen al recargar
- Posibles causas:
  a) Inconsistencia entre año tributario (2026) y año comercial (2025) en clave localStorage
  b) El auto-load no está ejecutándose correctamente
  c) Los datos se guardan en servidor pero no se recuperan correctamente
  d) Conflicto entre localStorage y datos del servidor

**PROBLEMA #2: Railway ephemeral storage**
- SQLite pierde datos en cada restart del servidor
- localStorage implementado como workaround temporal
- Se necesita migración a PostgreSQL o volumen persistente configurado correctamente

**PROBLEMA #3: Códigos F22 vacíos**
- Plan base tiene `cod_f22` vacío para la mayoría de cuentas
- Solo 52 de 258 cuentas tienen código SII válido después de verificación
- Necesita asignación manual o importación desde clasificación oficial SII

---

## Estado Actual de la Sesión (2026-06-30)

### Últimos cambios realizados (commit `8abc354`):
1. **Fix año comercial**: `getCommercialYear()` extrae año desde fecha de cierre
2. **Fix Guardar Cambios**: maneja tabla vacía sin error 400
3. **Fix backend**: acepta arrays vacíos en overrides
4. **Auto-load**: `cargarDatosDJ()` se ejecuta automáticamente al abrir página

### Situación actual:
- El usuario reporta que **sigue sin aparecer** el código de actividad económica y los folios
- El botón "Guardar Cambios" ya no da error (fix aplicado correctamente)
- Los datos parecen guardarse pero no se recuperan visualmente

---

## Diagnóstico del Problema de Persistencia

### Hipótesis principales:

1. **Desfase año comercial vs tributario**:
   - Año tributario DJ: 2026
   - Año comercial (fecha cierre): 2025 (31-12-2025)
   - localStorage key: `dj1847_{rut}_{año}` — ¿cuál año se usa?
   - El fix unificó a año comercial, pero puede haber datos guardados con año tributario anteriormente

2. **localStorage vs Servidor**:
   - El auto-load primero busca localStorage, luego servidor
   - Si hay datos stale en localStorage, nunca llega al servidor
   - Recomendación: limpiar localStorage y probar flujo limpio

3. **Fecha de cierre dinámica**:
   - `$('#dj-fecha').val()` puede estar vacío al momento del auto-load
   - Si está vacío, fallback a `$('#dj-periodo').val()` (2026)
   - Esto crearía inconsistencia en la clave

4. **Railway data loss**:
   - Si el servidor se reinició, los datos SQLite se perdieron
   - localStorage debería ser el fallback, pero la clave puede ser incorrecta

### Próximos pasos de diagnóstico:
1. Abrir DevTools → Application → LocalStorage → ver claves `dj1847_*`
2. Verificar si hay datos guardados con clave 2025 vs 2026
3. Probar flujo: limpiar localStorage → ingresar datos → guardar → recargar
4. Verificar en servidor (si es posible) si los datos llegan a SQLite

---

## Pendientes para Próxima Sesión

### CRÍTICO (Bloqueante):
1. [ ] **Resolver persistencia de datos Sección B** — Los datos no aparecen al recargar
   - Opciones: a) Debug localStorage, b) Cambiar a año tributario consistente, c) Implementar PostgreSQL
   
2. [ ] **Migrar a PostgreSQL** — Railway ofrece PostgreSQL como addon; SQLite no es viable en producción
   - Crear addon PostgreSQL en Railway
   - Migrar esquema de tablas existentes
   - Actualizar `get_connection()` para usar PostgreSQL cuando esté disponible

3. [ ] **Completar Códigos F22** — Necesario para CSV SII válido
   - Asignar manualmente desde `clasificacion_sii_dj1847.xlsx`
   - O importar automáticamente si el archivo tiene los códigos

### IMPORTANTE:
4. [ ] **Validar CSV SII** — El CSV generado debe ser aceptado por el importador del SII
   - Verificar formato exacto: 21 columnas, separador `;`, codificación UTF-8
   - Verificar correlativo reinicia por sección
   - Probar importación en sitio de pruebas SII

5. [ ] **Folios usados** — Implementar tracking de folios para evitar duplicados

### MEJORAS:
6. [ ] **Tests automáticos** — Agregar tests para DJ1847, especialmente CSV export
7. [ ] **Documentación** — Actualizar instrucciones para usuario final
8. [ ] **DJ 1926** — Revisar si necesita ajustes similares a DJ 1847

---

## Notas Técnicas para Continuación

### Estructura de claves localStorage:
```
dj1847_{rut}_{periodo}  → datos Sección B (actividad, folios, etc.)
```

### Endpoints API relevantes:
- `GET /api/dj1847_periodo/{rut}?periodo={año}` — Cargar datos Sección B
- `POST /api/dj1847_periodo/{rut}` — Guardar datos Sección B
- `POST /api/dj1847_overrides/{rut}` — Guardar Cód. F22 y Valor Tributario
- `GET /api/exportar_dj1847_csv/{rut}?fecha={fecha}&periodo={periodo}` — Exportar CSV SII

### Archivos clave modificados recientemente:
- `templates/dj1847.html` — Frontend con localStorage
- `app.py` — Endpoints API y lógica CSV
- `core/db.py` — Funciones de base de datos

### Comando útil para debug:
```javascript
// En consola del navegador:
// Ver todas las claves DJ1847 en localStorage
Object.keys(localStorage).filter(k => k.startsWith('dj1847_'))

// Ver contenido específico
localStorage.getItem('dj1847_799171805_2025')
localStorage.getItem('dj1847_799171805_2026')

// Limpiar todo
localStorage.clear()
```

---

## Contexto del Usuario

- Empresa: PUBLICIDAD LOCUCION EVENTOS Y SERVICIOS (RUT 799171805)
- Año Tributario: 2026
- Fecha Cierre: 31-12-2025
- Actividad Económica: (código de 6 dígitos, pendiente)
- Supervisor: NO APLICA
- Ajustes RLI: 2-NO
- Folios: (pendientes)

El usuario tiene un CSV `DJ1847_799171805_2026_v4.csv` que fue aceptado por SII después de fixes manuales. Este es el benchmark a replicar automáticamente.
