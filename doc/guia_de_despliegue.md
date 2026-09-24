# Guía de despliegue — Facturación electrónica SII (TF)

Cómo instalar, actualizar y operar los módulos `tf_dte_cl`, `tf_l10n_cl` y
`tf_dte_cl_intercambio` en Odoo 18. Recoge los problemas encontrados durante la
puesta en marcha y cómo evitarlos.

> Las rutas corresponden a la instalación de referencia. Si la tuya es distinta,
> ajústalas: la ruta de Odoo, el entorno virtual, la carpeta de módulos, el
> archivo de configuración y el nombre del servicio.

| Elemento | Ruta o valor de referencia |
|---|---|
| Odoo | `/opt/odoo/odoo` (contiene `odoo-bin`) |
| Entorno virtual de Python | `/opt/odoo/odoo-venv` |
| Módulos propios | `/opt/odoo/odoo-custom-addons` |
| Configuración | `/etc/odoo.conf` |
| Servicio | `odoo` (systemd), usuario del sistema `odoo` |
| Base de producción | `odoo` |
| Base de pruebas (solo en desarrollo) | `odoo_tests` |

Para no repetir la ruta completa en cada comando:

```bash
ODOO="sudo -u odoo /opt/odoo/odoo-venv/bin/python /opt/odoo/odoo/odoo-bin -c /etc/odoo.conf"
```

---

## 1. Componentes

| Componente | Versión | Notas |
|---|---|---|
| Odoo | 18.0 Community | |
| `facturacion_electronica` | **0.24.0**, fija | `pip install facturacion-electronica==0.24.0` en el entorno virtual de Odoo. Otra versión puede cambiar las respuestas que interpreta el módulo |
| `tf_dte_cl` | 18.0.2.x | Emisión de DTE |
| `tf_l10n_cl` | 18.0.1.x | Plan de cuentas «Chile (TF)» |
| `tf_dte_cl_intercambio` | 18.0.5.x | Intercambio con clientes y proveedores |
| `account_usability` (OCA) | 18.0 | Recomendado en Community: muestra el plan de cuentas |

**No instalar** `l10n_cl`, `l10n_latam_base` ni `l10n_latam_invoice_document`.

---

## 2. Instalación en una base nueva

1. Crear la base **sin datos de demostración y sin país**, para que Odoo no
   instale `l10n_cl` al instalar Contabilidad.
2. Instalar `tf_dte_cl`.
3. En la compañía: país Chile, RUT, dirección y **moneda CLP** (antes de
   registrar cualquier asiento).
4. Instalar `tf_l10n_cl` y elegir **Chile (TF)** en *Facturación > Ajustes >
   Localización fiscal*.
5. Instalar `tf_dte_cl_intercambio` y `account_usability`.
6. Verificar en Aplicaciones que `l10n_cl` y los `l10n_latam_*` **no** estén instalados.
7. Configurar según los manuales de uso de cada módulo (`doc/manual_de_uso.md`).

---

## 3. Actualizar producción

### 3.1 Regla principal

**Si una versión agrega campos, se actualiza desde la consola con el servicio
detenido, nunca desde Aplicaciones.**

Si el servicio se reinicia con el código nuevo antes de actualizar la base, Odoo
intenta leer columnas que todavía no existen y falla en cualquier pantalla,
incluida Aplicaciones (error `no existe la columna ...`). Las notas de cada
versión indican si agrega campos; ante la duda, usar siempre este procedimiento.

Las versiones que solo cambian vistas, reportes o lógica se pueden actualizar
desde Aplicaciones.

### 3.2 Procedimiento

**1. Probar en desarrollo** (ver la sección 4). No se despliega nada que no
haya pasado las pruebas.

**2. Respaldar la base y los archivos adjuntos.** El *filestore* está en la
carpeta `data_dir` del archivo de configuración, dentro de `filestore/<base>`.

```bash
FECHA=$(date +%F_%H%M)
sudo -u postgres pg_dump -Fc odoo > /var/backups/odoo/odoo_$FECHA.dump
grep data_dir /etc/odoo.conf           # ubicación del filestore
sudo tar -czf /var/backups/odoo/filestore_$FECHA.tar.gz -C <data_dir>/filestore odoo
```

**3. Reemplazar el código** de los módulos en `/opt/odoo/odoo-custom-addons`
(desde el repositorio o descomprimiendo el zip). Reemplazar la carpeta completa
del módulo, no archivos sueltos: así no quedan archivos de una versión anterior.

**4. Actualizar con el servicio detenido:**

```bash
sudo systemctl stop odoo
$ODOO -d odoo -u tf_dte_cl,tf_l10n_cl,tf_dte_cl_intercambio \
    --stop-after-init --logfile=/tmp/odoo_actualizacion.log
grep -nE "ERROR|CRITICAL|Traceback" /tmp/odoo_actualizacion.log
sudo systemctl start odoo
```

Si el `grep` muestra errores, **no iniciar el servicio**: revisar el log y, si
hace falta, volver atrás (sección 3.4).

**5. Verificar:**

- en Aplicaciones, que cada módulo muestre la versión nueva;
- en *Ajustes > Técnico > Tareas programadas*, que las tareas DTE estén activas;
- abrir una factura con DTE y usar **Consultar SII** para confirmar que la
  conexión con el SII sigue funcionando.

**6. Etiquetar en el repositorio** la versión desplegada:

```bash
git tag -a v18.0.2.6.2 -m "Desplegada en producción el <fecha>"
git push origin v18.0.2.6.2
```

### 3.3 Cuándo hacerlo

Con el servicio detenido no se emiten documentos, así que conviene un momento
sin facturación. Los procesos automáticos también se detienen y retoman al
iniciar el servicio: los documentos pendientes se envían en el siguiente ciclo.

### 3.4 Volver atrás

1. Detener el servicio.
2. Restaurar el código de la versión anterior (por su etiqueta en el repositorio).
3. Restaurar la base y el filestore del respaldo:

   ```bash
   sudo -u postgres dropdb odoo
   sudo -u postgres createdb -O odoo odoo
   sudo -u postgres pg_restore -d odoo /var/backups/odoo/odoo_<FECHA>.dump
   sudo rm -rf <data_dir>/filestore/odoo
   sudo tar -xzf /var/backups/odoo/filestore_<FECHA>.tar.gz -C <data_dir>/filestore
   ```

4. Iniciar el servicio.

**Atención:** los documentos emitidos al SII entre el respaldo y la restauración
existen en el SII aunque desaparezcan de Odoo. Antes de restaurar una base que
ya emitió documentos, anotar sus folios: no se pueden volver a usar.

---

## 4. Pruebas automatizadas

Se corren en el **servidor de desarrollo**, en una base exclusiva, nunca en la de
producción. Los módulos deben estar en la misma versión que se va a desplegar.

```bash
cd /opt/odoo/odoo
dropdb --if-exists odoo_tests
./odoo-bin -c /etc/odoo.conf -d odoo_tests --without-demo=all \
    -i tf_dte_cl_intercambio --test-enable --test-tags /tf_dte_cl,/tf_dte_cl_intercambio \
    --http-port=8070 --logfile=/tmp/odoo_tests.log --stop-after-init
grep -E "failed, [0-9]+ error" /tmp/odoo_tests.log
```

Resultado esperado: `0 failed, 0 error(s) of 91 tests` (o el número vigente).

- `--http-port=8070` evita el choque con el servicio de desarrollo, que ya usa el 8069.
- `--logfile` es necesario si el archivo de configuración define `logfile`:
  si no, el resultado no aparece en pantalla.
- Las trazas `ConnectionError: reset` y `'NoneType' object has no attribute
  'text'` son esperadas: son errores que las propias pruebas simulan.

---

## 5. Consola de Odoo

Siempre con el archivo de configuración, o Odoo no carga los módulos propios
(error `Invalid field account.move.tf_dte_cl_folio`):

```bash
$ODOO shell -d odoo --no-http
```

Antes de modificar datos, comprobar que la consola quedó bien cargada:

```python
print(env.cr.dbname)
print(env['ir.module.module'].search([('name', '=', 'tf_dte_cl')]).state)   # installed
```

Pegar los comandos de a uno o en bloques pequeños, y no seguir si alguno falla.
Los cambios solo se guardan con `env.cr.commit()`.

---

## 6. Procesos automáticos

| Tarea | Frecuencia | Módulo |
|---|---|---|
| DTE: enviar facturas y notas pendientes al SII | 5 minutos | `tf_dte_cl` |
| DTE: enviar guías de despacho pendientes al SII | 5 minutos | `tf_dte_cl` |
| DTE: consultar estado de facturas y notas en el SII | 15 minutos | `tf_dte_cl` |
| DTE: consultar estado de guías de despacho en el SII | 15 minutos | `tf_dte_cl` |
| DTE: alerta de vencimiento de certificados digitales | Diaria | `tf_dte_cl` |
| DTE: responder la recepción de los sobres recibidos | 10 minutos | `tf_dte_cl_intercambio` |
| DTE: enviar documentos aceptados al receptor | 15 minutos | `tf_dte_cl_intercambio` |
| DTE: verificar en el SII los documentos recibidos | 30 minutos | `tf_dte_cl_intercambio` |
| DTE: consultar la respuesta de los clientes en el SII | 4 horas | `tf_dte_cl_intercambio` |
| DTE: avisar plazos de documentos recibidos | Diaria | `tf_dte_cl_intercambio` |

La tarea estándar de `account_edi` viene **inactiva** en Odoo 18 y no debe
activarse: el envío lo hacen las tareas del módulo.

---

## 7. Operación periódica

| Qué revisar | Dónde | Frecuencia sugerida |
|---|---|---|
| Documentos en error, rechazados o sin confirmar | Filtros *DTE con problemas* y *DTE pendientes* en facturas | Diaria |
| Folios disponibles de cada CAF | *Facturación electrónica Chile > CAF* (en naranja con 10 o menos) | Semanal |
| Folios por anular | *Folios por anular*: anularlos en el sitio del SII | Semanal |
| Vencimiento del certificado | Aviso automático 30 días antes | Al recibir el aviso |
| Documentos recibidos sin respuesta | Filtro *Sin respuesta SII* | Diaria |
| Facturas reclamadas por clientes | Filtro *Reclamadas por el cliente* | Diaria |
| Errores en el log | `grep -E "ERROR" <logfile>` | Semanal |

---

## 8. Problemas frecuentes

| Síntoma | Causa | Solución |
|---|---|---|
| `no existe la columna res_company.tf_dte_cl_...` en cualquier pantalla | El servicio corre el código nuevo sin haber actualizado la base | Actualizar desde la consola con el servicio detenido (sección 3.2) |
| `Invalid field ...tf_dte_cl_...` en la consola | Consola abierta sin `-c /etc/odoo.conf` | Abrirla con el archivo de configuración |
| `Port 8069 is in use` al correr pruebas | El servicio de Odoo está corriendo | Usar `--http-port=8070` |
| El comando termina sin mostrar resultados | La configuración define `logfile` | Revisar ese archivo o usar `--logfile` |
| Avisos de docutils al actualizar (`Unexpected indentation`) | Otro módulo tiene una descripción en reStructuredText mal formada | Son inofensivos |
| Error de vista `View inheritance may not use attribute 'string'` | Un `xpath` usa `@string` como selector | Usar `@name` o una ruta; lo detectan las pruebas |
| Error al imprimir `QWeb widgets do not work correctly on 'td' elements` | `t-field` directamente sobre una celda | Envolver el campo en un `<span>` |

---

## 9. Repositorio

- **Una rama por serie de Odoo** (por ejemplo, `18.0`) y **una etiqueta por
  versión desplegada**.
- **Mensajes de commit** con el formato `tipo(módulo): descripción (versión)`,
  por ejemplo `fix(tf_dte_cl): ... (18.0.2.6.2)`. Tipos: `feat`, `fix`,
  `docs`, `test`.
- **Nunca subir certificados, CAF ni XML reales**: agregar `*.p12`, `*.pfx` y
  los CAF al `.gitignore`. Las pruebas generan los suyos.
