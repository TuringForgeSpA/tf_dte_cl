# Manual de administrador — Facturación electrónica SII (TF)

Nov 3, 2026 · @César

## 1. Componentes y requisitos

Este manual cubre la instalación, la configuración, el despliegue y la operación técnica. El uso diario está en el **Manual de usuario**.

| Componente | Versión | Depende de | Para qué |
| --- | --- | --- | --- |
| Odoo | 18.0 Community |  |  |
| `facturacion_electronica` (Python) | **0.24.0, fija** |  | Arma, timbra, firma y envía los DTE. Otra versión puede cambiar las respuestas que interpretan los módulos |
| `tf_dte_cl` | 18.0.2.7.x | `account`, `account_edi`, `stock`, `sale_stock`, `contacts` | Emisión de 33, 34, 52, 56 y 61 |
| `tf_l10n_cl` | 18.0.1.x | `account`, `tf_dte_cl` | Plan de cuentas «Chile (TF)», impuestos, posiciones fiscales y bancos |
| `tf_dte_cl_intercambio` | 18.0.6.x | `tf_dte_cl` | Intercambio con clientes y proveedores |
| `tf_dte_cl_cesion` | 18.0.1.0.x | `tf_dte_cl_intercambio` | Cesión de facturas (AEC) |
| `tf_l10n_cl_f29` | 18.0.1.0.x | `tf_l10n_cl`, `tf_dte_cl_intercambio` | Cálculo del F29 y comparación con el RCV |
| `account_usability` (OCA) | 18.0 |  | Recomendado en Community: muestra el plan de cuentas y menús contables |

**No instalar** `l10n_cl`, `l10n_cl_edi`, `l10n_latam_base` ni `l10n_latam_invoice_document`: agregan campos de *Tipo de documento* y *Número de documento* que estos módulos no usan y que inducen a ingresar el folio a mano.

### Rutas de referencia

| Elemento | Valor |
| --- | --- |
| Odoo | `/opt/odoo/odoo` (contiene `odoo-bin`) |
| Entorno virtual | `/opt/odoo/odoo-venv` |
| Módulos propios | `/opt/odoo/odoo-custom-addons` |
| Configuración | `/etc/odoo.conf` |
| Servicio | `odoo` (systemd), usuario `odoo` |
| Base de producción | `odoo` |
| Base de pruebas (solo desarrollo) | `odoo_tests` |

Ajusta las rutas si tu instalación es distinta. Para abreviar los comandos:

```bash
ODOO="sudo -u odoo /opt/odoo/odoo-venv/bin/python /opt/odoo/odoo/odoo-bin -c /etc/odoo.conf"
```

### Requisitos del servidor

- `pip install facturacion-electronica==0.24.0` en el entorno virtual de Odoo.
- `wkhtmltopdf` con parche de Qt para la impresión.
- Salida a internet hacia los servidores del SII (`maullin.sii.cl` en certificación, `palena.sii.cl` en producción) y hacia el servidor de correo.
- OpenSSL 3 con el proveedor *legacy* si hay certificados `.p12` antiguos cifrados con RC2-40-CBC.

## 2. Instalación en una base nueva

Odoo instala `l10n_cl` por su cuenta si Contabilidad se instala en una compañía chilena. Para evitarlo, sigue este orden:

1. Crea la base **sin datos de demostración y sin país** (o con uno distinto de Chile).
2. Instala `tf_dte_cl`. Instala Contabilidad con el plan genérico.
3. En la compañía: país **Chile**, RUT, dirección y moneda **CLP**. La moneda debe cambiarse **antes de registrar cualquier asiento**.
4. Instala `tf_l10n_cl` y elige **Chile (TF)** en *Facturación > Ajustes > Localización fiscal*. Si no aparece en el selector, cárgalo desde la consola:

   ```python
   env['account.chart.template'].try_loading('cl_tf', env.company)
   env.cr.commit()
   ```
5. Instala `tf_dte_cl_intercambio`, `tf_dte_cl_cesion`, `tf_l10n_cl_f29` y, en Community, `account_usability`.
6. Verifica en Aplicaciones que `l10n_cl` y los `l10n_latam_*` **no** estén instalados.
7. Configura según las secciones 3 a 5.

Al instalar se cargan solos, sin duplicar registros existentes: 346 **comunas** con su código y región, 674 **actividades económicas** del SII y el catálogo de **tipos de documento de referencia**. Si una comuna no se empareja con su región, queda sin región y se informa en el log.

> Mantén los mismos módulos instalados en producción y en desarrollo (incluidos los estándar, como `base_vat`): un módulo adicional en un solo ambiente puede cambiar las vistas y ocultar diferencias hasta llegar a producción.

## 3. Configuración de la compañía y del SII

Se hace una vez por compañía, con permisos de administrador.

### 3.1 Datos de la compañía

En *Ajustes > Compañías*: RUT (se valida el dígito verificador), dirección, ciudad y moneda **CLP**. En *Contabilidad > Ajustes*, redondeo de impuestos **global**: el SII calcula el IVA sobre el total, no por línea. Con el plan «Chile (TF)» ya viene así.

### 3.2 Facturación electrónica

En *Contabilidad > Ajustes > Facturación electrónica Chile*:

| Campo | Qué poner |
| --- | --- |
| Ambiente SII | **Certificación** hasta que el SII autorice la emisión en producción |
| N° y fecha de resolución | Los de la resolución que autoriza a emitir. En certificación, N° 0 |
| Unidad regional SII | Texto bajo el folio, por ejemplo `S.I.I. - SANTIAGO CENTRO` |
| Giro, comuna, correo DTE | Giro de la empresa, comuna de la casa matriz y casilla de intercambio registrada en el SII |
| Actecos | Hasta 4 actividades económicas |
| Vigencia de CAF | Meses tras los cuales un CAF deja de usarse (6 por defecto; criterio propio, confirma el plazo vigente) |
| Documento por defecto | 33 o 34 |
| Formato de impresión | A4/carta o térmico 80 mm |
| Imprimir copia cedible | Activo por defecto |

Arriba del bloque aparece el **estado de la configuración** con lo que falta; se actualiza al guardar.

### 3.3 Certificado digital

1. En el bloque *Certificado digital*, usa **Cargar certificado**.
2. Adjunta el `.p12` o `.pfx`, ingresa la contraseña y usa **Cargar**.

Se extraen el certificado, la llave y el RUT del firmante. **El archivo y la contraseña no se guardan**, y la llave solo es visible para administradores del sistema. Si el certificado no informa el RUT del firmante, complétalo en su ficha. El firmante debe ser un usuario autorizado por la empresa ante el SII (si no, el SII responde código 17 en aceptaciones y reclamos).

Se avisa **30 días antes del vencimiento** con una actividad al responsable.

Si un `.p12` antiguo no abre, puede estar cifrado con RC2-40-CBC, que OpenSSL 3 solo admite con el proveedor *legacy*. Es un ajuste del servidor: habilita el proveedor en la configuración de OpenSSL. No reexportes el certificado sin avisar al titular.

### 3.4 CAF

Descarga los CAF desde el sitio del SII (uno por tipo de documento) y cárgalos en *Contabilidad > Configuración > Facturación electrónica Chile > CAF*: **Nuevo**, adjunta el archivo y guarda.

- Se completan tipo, RUT, rango y fecha, y se valida que el CAF sea de la compañía, que no se solape con otro y que sus llaves sean coherentes.
- El **ambiente** se deduce del `IDK` del CAF (100 certificación, 300 producción). Solo se toman folios de CAF del ambiente configurado; un CAF de certificación y uno de producción pueden tener los mismos folios.
- Con 10 folios o menos, la fila se muestra en naranja.
- La toma de folios bloquea la fila del CAF: dos usuarios que confirman a la vez nunca reciben el mismo folio, y un folio firmado nunca se reutiliza.

No guardes CAF reales en el repositorio.

### 3.5 Impuestos de venta

Con `tf_l10n_cl` vienen con su **código SII**. Si creas uno a mano, asígnalo en *Contabilidad > Configuración > Impuestos*:

| Código | Impuesto | Tasa |
| --- | --- | --- |
| 14 | IVA | 19 % |
| 17 | IVA anticipado faenamiento de carne | 5 % |
| 18 | IVA anticipado carne | 5 % |
| 24 | Licores, piscos, whisky y similares | 31,5 % |
| 25 | Vinos | 20,5 % |
| 26 | Cervezas y bebidas alcohólicas | 20,5 % |
| 27 | Bebidas analcohólicas y minerales | 10 % |
| 271 | Bebidas con elevado contenido de azúcares | 18 % |

La tasa debe ser la oficial y el impuesto **no puede estar incluido en el precio**.

### 3.6 Diarios de venta

Un diario por tipo: en *Contabilidad > Configuración > Diarios*, indica el **Tipo DTE** (33, 34, 56 o 61) y, si corresponde, la **Sucursal SII**. Las notas de crédito necesitan su propio diario (61).

### 3.7 Sucursales

Si emites desde sucursales registradas en el SII, créalas en *Facturación electrónica Chile > Sucursales* con nombre y **código SII** (numérico, hasta 9 dígitos). Si la sucursal tiene dirección, comuna y ciudad completas, el DTE informa esa dirección de origen; si no, la de la compañía.

### 3.8 Tipos de operación (guías)

En *Inventario > Configuración > Tipos de operación*, en las entregas o traslados internos que emiten guía: **Tipo DTE** 52, sucursal, e indicador de traslado y tipo de despacho por defecto.

## 4. Configuración del intercambio

### 4.1 Casilla de intercambio

Odoo necesita una casilla **propia y exclusiva** que pueda leer. Configuración recomendada, sin costo adicional:

1. **Cuenta de Gmail dedicada** (por ejemplo, `intercambio.dte.tf@gmail.com`). Actívale la verificación en dos pasos y genera una **contraseña de aplicación** en `myaccount.google.com/apppasswords`. Guárdala: solo se muestra una vez.
2. **Grupo de Google Workspace** con tu dominio (por ejemplo, `intercambio@turingforge.cl`) con la cuenta de Gmail como miembro. Permite **miembros externos** y que **cualquier persona externa pueda publicar**; si no, los correos de los proveedores rebotan.
3. **Registro en el SII**: actualiza el correo de intercambio de la empresa a la dirección del grupo, y usa la misma dirección en el **Correo DTE** de la compañía.

Todo lo que llega a la casilla se procesa; un correo sin DTE queda como sobre con error.

### 4.2 Servidores de correo

En *Ajustes > Técnico > Correo electrónico*:

| Servidor | Datos |
| --- | --- |
| **Entrante** | IMAP, `imap.gmail.com`, puerto 993, SSL/TLS, cuenta de Gmail y contraseña de aplicación. En *Crear un nuevo registro* elige **Sobre DTE recibido**. Usa **Probar y confirmar** |
| **Saliente** | `smtp.gmail.com`, puerto 465 con SSL (o 587 con STARTTLS), mismas credenciales |

Verifica que la tarea estándar de descarga de correos entrantes esté activa. Los correos salen con la dirección de Gmail como remitente; no afecta al intercambio, porque el receptor valida el RUT del XML.

### 4.3 Ajustes del módulo

En *Contabilidad > Ajustes > Facturación electrónica Chile*, bloque **Intercambio de documentos**:

| Ajuste | Para qué |
| --- | --- |
| Enviar el DTE al receptor | Envía solos los documentos aceptados por el SII a los clientes con correo de intercambio |
| Producto para documentos recibidos | Producto genérico de las líneas de las facturas de proveedor (viene creado: «Producto de proveedor») |
| Responsable de documentos recibidos | Recibe los avisos de plazo de documentos que aún no tienen factura de proveedor |
| Aviso de plazo (días antes) | Anticipación de esos avisos; 2 por defecto |

## 5. Configuración de cesión y F29

### 5.1 Cesión

- Usa el mismo certificado de la compañía; su titular debe ser representante o usuario autorizado de la empresa.
- Marca como **Factoring** los contactos cesionarios, con RUT, razón social, dirección y correo.
- El acuse de recibo que exige la cesión viene del seguimiento de `tf_dte_cl_intercambio`: la tarea *consultar la respuesta de los clientes* debe estar activa.
- **Prueba siempre en certificación**: una cesión en producción transfiere legalmente el cobro y no se puede anular. Usa una base de desarrollo con la compañía en certificación, un CAF de certificación y una factura emitida allí.

### 5.2 F29

- **Impuestos:** con el plan «Chile (TF)» cada impuesto ya tiene su **Categoría F29**. Con otro plan, asígnala en cada impuesto (IVA débito, IVA crédito del giro, activo fijo, no recuperable, retención de honorarios, etc.).
- **Posiciones fiscales:** usa las de IVA no recuperable y activo fijo para que las compras caigan en los códigos 564/521 y 524/525.
- **Facturas de proveedor** creadas a mano: deben llevar *Tipo de documento SII (compra)* y *Folio del proveedor*; sin ellos no se clasifican ni se comparan con el RCV.

## 6. Permisos y seguridad

| Permiso o regla | Dónde | Efecto |
| --- | --- | --- |
| Grupos de Facturación / Contabilidad | Ficha del usuario | Emitir, ver documentos recibidos, cesiones y F29 |
| **DTE: puede restablecer documentos electrónicos** | Ficha del usuario | Muestra **Restablecer DTE**. Asígnalo solo a quien decide reemitir con folio nuevo |
| Administrador del sistema | Ficha del usuario | Único que ve certificados y llaves |
| Show Full Accounting Features | Ficha del usuario (con `account_usability`) | Muestra el plan de cuentas en Community |

Reglas de seguridad que aplican los módulos y que debes mantener:

- El `.p12` y su contraseña **no se guardan**; la llave privada solo se usa para firmar.
- Los logs informan a lo más tipo de documento, folio y Track ID; nunca el XML completo, la llave ni la contraseña.
- La descarga de XML pasa por los permisos de Odoo sobre el documento.
- **Nunca subas al repositorio** certificados, CAF ni XML reales: agrega `*.p12`, `*.pfx` y los CAF al `.gitignore`. Las pruebas generan los suyos.
- No entregues el `.p12` a servicios de terceros.

## 7. Procesos automáticos

En *Ajustes > Técnico > Tareas programadas*. Para probar sin esperar, abre la tarea y usa **Ejecutar manualmente**.

| Tarea | Frecuencia | Módulo |
| --- | --- | --- |
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
| DTE: enviar y consultar cesiones de facturas | 15 minutos | `tf_dte_cl_cesion` |

La tarea estándar de `account_edi` viene **inactiva** en Odoo 18 y **no debe activarse**: el envío lo hacen las tareas del módulo, que mantienen sincronizado el documento EDI.

### Envío idempotente

Cada envío se registra como intento en una transacción propia antes de llamar al SII. Si no hay respuesta, el documento queda en **Envío sin confirmar** y, antes de reenviar, se consulta al SII si ya lo tiene. El reenvío espera **60 minutos**, valor del parámetro del sistema `tf_dte_cl.unknown_resend_minutes` (*Ajustes > Técnico > Parámetros del sistema*). No lo bajes a pocos minutos: el SII puede tardar en reflejar un envío.

## 8. Paso de certificación a producción

Mientras la empresa no esté autorizada, todo se hace en **certificación**. Los documentos emitidos allí no tienen validez tributaria y sus folios no cuentan en producción.

Cuando el SII autoriza la emisión:

1. Respalda la base (sección 9.2).
2. En *Ajustes > Facturación electrónica Chile*, cambia el **Ambiente SII** a **Producción** e ingresa el **número y la fecha de la resolución** definitiva.
3. Descarga desde el sitio del SII los **CAF de producción** de cada tipo y cárgalos. La columna *Ambiente* debe decir Producción. Los CAF de certificación pueden quedar cargados: no se usan en producción.
4. Verifica que el **correo de intercambio** registrado en el SII sea el de la casilla configurada.
5. Emite un primer documento real de bajo riesgo y sigue su estado hasta **Aceptado**; revisa el PDF.

Para pruebas posteriores (por ejemplo, de cesión), usa una **base de desarrollo en certificación**, nunca la de producción cambiada de ambiente.

## 9. Actualizaciones, respaldo y vuelta atrás

### 9.1 Regla principal

**Si una versión agrega campos, se actualiza desde la consola con el servicio detenido, nunca desde Aplicaciones.** Si el servicio arranca con el código nuevo antes de actualizar la base, Odoo busca columnas que no existen y falla en todas las pantallas (`no existe la columna ...`), incluida Aplicaciones. Ante la duda, usa siempre este procedimiento. Las versiones que solo cambian vistas, reportes o lógica se pueden actualizar desde Aplicaciones.

### 9.2 Procedimiento

1. **Prueba en desarrollo** (sección 10). No se despliega nada que no haya pasado las pruebas.
2. **Respalda la base y el filestore** (carpeta `data_dir` de la configuración, `filestore/<base>`):

   ```bash
   FECHA=$(date +%F_%H%M)
   sudo -u postgres pg_dump -Fc odoo > /var/backups/odoo/odoo_$FECHA.dump
   grep data_dir /etc/odoo.conf
   sudo tar -czf /var/backups/odoo/filestore_$FECHA.tar.gz -C <data_dir>/filestore odoo
   ```
3. **Reemplaza la carpeta completa** de cada módulo en `/opt/odoo/odoo-custom-addons`, no archivos sueltos.
4. **Actualiza con el servicio detenido:**

   ```bash
   sudo systemctl stop odoo
   $ODOO -d odoo -u tf_dte_cl,tf_l10n_cl,tf_dte_cl_intercambio,tf_dte_cl_cesion,tf_l10n_cl_f29 \
       --stop-after-init --logfile=/tmp/odoo_actualizacion.log
   grep -nE "ERROR|CRITICAL|Traceback" /tmp/odoo_actualizacion.log
   sudo systemctl start odoo
   ```

   Si el `grep` muestra errores, **no inicies el servicio**: revisa el log y, si hace falta, vuelve atrás.
5. **Verifica:** versiones nuevas en Aplicaciones, tareas DTE activas y **Consultar SII** en una factura para confirmar la conexión.
6. **Etiqueta** la versión desplegada en el repositorio (`git tag -a v18.0.x.y.z`).

Hazlo en un momento sin facturación: con el servicio detenido no se emite. Los documentos pendientes se envían en el siguiente ciclo.

### 9.3 Volver atrás

1. Detén el servicio.
2. Restaura el código de la versión anterior (por su etiqueta).
3. Restaura la base y el filestore:

   ```bash
   sudo -u postgres dropdb odoo
   sudo -u postgres createdb -O odoo odoo
   sudo -u postgres pg_restore -d odoo /var/backups/odoo/odoo_<FECHA>.dump
   sudo rm -rf <data_dir>/filestore/odoo
   sudo tar -xzf /var/backups/odoo/filestore_<FECHA>.tar.gz -C <data_dir>/filestore
   ```
4. Inicia el servicio.

> **Atención:** los documentos emitidos entre el respaldo y la restauración existen en el SII aunque desaparezcan de Odoo, y sus folios no se pueden volver a usar. Antes de restaurar una base que ya emitió documentos, anota esos folios y no restaures sin un plan para no repetirlos; si tienes dudas, prefiere corregir hacia adelante en vez de restaurar.

## 10. Pruebas automatizadas y consola

### 10.1 Pruebas

Las pruebas simulan las respuestas del SII y generan su propio certificado y CAF. Se corren en el **servidor de desarrollo**, en una base exclusiva, con los módulos en la versión que se va a desplegar:

```bash
cd /opt/odoo/odoo
dropdb --if-exists odoo_tests
./odoo-bin -c /etc/odoo.conf -d odoo_tests --without-demo=all \
    -i tf_l10n_cl_f29,tf_dte_cl_cesion \
    --test-enable --test-tags /tf_dte_cl,/tf_l10n_cl,/tf_dte_cl_intercambio,/tf_dte_cl_cesion,/tf_l10n_cl_f29 \
    --http-port=8070 --logfile=/tmp/odoo_tests.log --stop-after-init
grep -E "failed, [0-9]+ error" /tmp/odoo_tests.log
```

Resultado esperado: `0 failed, 0 error(s)` sobre el total vigente (146 pruebas a la fecha).

- `--http-port=8070` evita el choque con el servicio, que usa el 8069.
- `--logfile` es necesario si la configuración define `logfile`; si no, el resultado no aparece en pantalla.
- Las trazas `ConnectionError: reset` y `'NoneType' object has no attribute 'text'` son esperadas: son errores que las pruebas simulan.
- **Nunca** corras las pruebas en la base de producción.

### 10.2 Consola de Odoo

Siempre con el archivo de configuración, o no se cargan los módulos propios (`Invalid field account.move.tf_dte_cl_folio`):

```bash
$ODOO shell -d odoo --no-http
```

Antes de modificar datos, comprueba la carga:

```python
print(env.cr.dbname)
print(env['ir.module.module'].search([('name', '=', 'tf_dte_cl')]).state)   # installed
```

- Pega los comandos de a uno o en bloques pequeños y no sigas si alguno falla.
- Los cambios solo se guardan con `env.cr.commit()`.
- Si actualizaste módulos, **cierra y vuelve a abrir la consola**: una consola abierta antes de la actualización sigue con el código anterior.

## 11. Operación periódica

| Qué revisar | Dónde | Frecuencia |
| --- | --- | --- |
| Documentos en error, rechazados o sin confirmar | Filtros *DTE con problemas* y *DTE pendientes* en facturas | Diaria |
| Documentos recibidos sin respuesta | Filtro *Sin respuesta SII* | Diaria |
| Facturas reclamadas por clientes | Filtro *Reclamadas por el cliente* | Diaria |
| Sobres recibidos con error | *Sobres recibidos* | Semanal |
| Folios disponibles de cada CAF | *CAF* (en naranja con 10 o menos) | Semanal |
| Folios por anular | *Folios por anular*: anularlos en el sitio del SII | Semanal |
| Errores en el log | `grep -E "ERROR" <logfile>` | Semanal |
| Vencimiento del certificado | Aviso automático 30 días antes | Al recibir el aviso |
| F29 y comparación con el RCV | *Declaraciones F29* | Mensual |
| Respaldos de base y filestore | Servidor | Según política; siempre antes de actualizar |

## 12. Problemas frecuentes

| Síntoma | Causa | Solución |
| --- | --- | --- |
| `no existe la columna ...tf_dte_cl_...` en cualquier pantalla | El servicio corre código nuevo sin actualizar la base | Actualizar desde la consola con el servicio detenido (9.2) |
| `Invalid field ...tf_dte_cl_...` en la consola | Consola sin `-c /etc/odoo.conf` | Abrirla con el archivo de configuración |
| La consola no ve un cambio recién desplegado | Se abrió antes de actualizar | Cerrarla y abrirla de nuevo |
| `Port 8069 is in use` al correr pruebas | El servicio está corriendo | Usar `--http-port=8070` |
| El comando termina sin mostrar resultados | La configuración define `logfile` | Revisar ese archivo o usar `--logfile` |
| Documentos que quedan en *Firmado* | Tareas programadas inactivas o servicio sin salida al SII | Revisar las tareas y la conectividad |
| El `.p12` no abre | Cifrado RC2-40-CBC con OpenSSL 3 | Habilitar el proveedor *legacy* |
| Error al cargar un CAF por solapamiento | Ya hay un CAF del mismo tipo, ambiente y rango | Revisar la lista de CAF; el ambiente lo determina el IDK |
| Faltan etiquetas en la ficha del contacto | Otro módulo (por ejemplo `base_vat`) cambia la vista | Actualizar `tf_dte_cl` a la versión vigente y alinear módulos entre ambientes |
| No llegan documentos de proveedores | Prueba IMAP fallida o tarea de correo entrante inactiva | Revisar el servidor entrante |
| Los proveedores reportan rebotes | El grupo no admite remitentes externos | Permitir publicación externa |
| SII responde código 17 al aceptar o reclamar | El certificado no es de un usuario autorizado | Usar el de un representante o usuario autorizado |
| Error de vista `View inheritance may not use attribute 'string'` | Un `xpath` usa `@string` | Usar `@name` o una ruta |
| `QWeb widgets do not work correctly on 'td' elements` | `t-field` sobre una celda | Envolverlo en un `<span>` |
| Avisos de docutils al actualizar | Descripción mal formada de otro módulo | Inofensivos |

## 13. Límites conocidos y criterios propios

### 13.1 Límites de `facturacion_electronica` 0.24.0

- **Referencias globales** (`IndGlobal`) y **RUT de otro contribuyente** (`RUTOtr`): la librería no los transmite; están bloqueados.
- **Un solo impuesto adicional** por línea.
- **Descuentos y recargos globales**: la librería los resta también del IVA; el módulo informa neto, IVA y total explícitos para corregirlo, y no los admite con impuestos adicionales.
- **Cesión**: la librería repite el ID `DocCed_<folio>` y arma la declaración jurada con «None»; el módulo usa un ID propio (`TFAEC<id>`) y su propia declaración. El estado se consulta sin `IdCesion`; en certificación el SII responde «Documento Cedido» sin tenedor, y la cesión queda *Confirmada por el SII*.
- La versión de la librería está **fija**: cambiarla exige correr toda la suite de pruebas.

### 13.2 Criterios propios (no definidos por el SII)

- Vigencia de CAF configurable (6 meses por defecto).
- Una nota debe llevar `CodRef` en al menos una referencia a un documento tributario.
- Sin Track ID, un documento con estado `DOK` (o modificado por una nota) se da por aceptado.
- Guías valorizadas: precio de venta, precio de lista o 1 (con aviso).
- Copia cedible de guías solo con indicador de traslado 1.
- Documentos recibidos «no recibidos por el SII» se siguen verificando durante 10 días desde la emisión.
- Asociación de guías recibidas por fecha: hasta 7 días y solo con una única recepción candidata.

### 13.3 Fuera de alcance

Boletas (39/41), punto de venta, factura de compra (46), liquidación-factura (43), exportación (110/111/112), libros electrónicos, IVA de uso común en el F29 y presentación automática del F29. La obtención automática de CAF queda para un servicio aparte.
