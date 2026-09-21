# tf_dte_cl — Facturación electrónica SII Chile (núcleo B2B)

Módulo técnico para Odoo 18 que emite documentos tributarios electrónicos (DTE)
ante el Servicio de Impuestos Internos (SII) de Chile usando la librería
[`facturacion_electronica`](https://gitlab.com/dansanti/facturacion_electronica) 0.24.0.

Versión 18.0.2.3.0. Es independiente de la localización oficial de Odoo
(`l10n_cl`, `l10n_latam_base`, `l10n_latam_invoice_document`).

## Alcance

| Documento | Código | Modelo |
|---|---|---|
| Factura electrónica | 33 | `account.move` |
| Factura no afecta o exenta electrónica | 34 | `account.move` |
| Nota de débito electrónica | 56 | `account.move` |
| Nota de crédito electrónica | 61 | `account.move` |
| Guía de despacho electrónica | 52 | `stock.picking` |

Fuera de alcance: boletas (39/41), punto de venta, factura de compra (46),
liquidación-factura (43), exportación (110/111/112), RCOF, libros y cesión.
El intercambio de DTE con el receptor está planificado para una segunda fase.

## Módulos relacionados

| Módulo | Para qué | Recomendación |
|---|---|---|
| **`tf_l10n_cl`** | Plan de cuentas, impuestos, grupos de impuestos, posiciones fiscales, reporte de impuestos, bancos y contactos del SII y la Tesorería, como paquete de localización **«Chile (TF)»**. Deja los impuestos de venta con su código SII listo para emitir. | Instalar junto con este módulo |
| **`account_usability`** (OCA, repositorio `account-financial-tools`) | En Odoo Community, el plan de cuentas y otros menús contables están ocultos tras el grupo técnico «Show Full Accounting Features». Este módulo los muestra y permite asignar el grupo desde la ficha del usuario. | Recomendado en Community |

### Por qué no usar `l10n_cl`

El plan de cuentas oficial viene en `l10n_cl`, que a su vez instala
`l10n_latam_base` y `l10n_latam_invoice_document`. Estos módulos agregan a
facturas, diarios y contactos campos propios como *Tipo de documento* y
*Número de documento*, que no usa este módulo y que llevan a pensar que el folio
se ingresa a mano. En este módulo el folio se asigna automáticamente al
confirmar y se muestra en el campo **Folio SII**.

`tf_l10n_cl` reemplaza a `l10n_cl` en lo contable, a partir de la misma
plantilla oficial de Odoo 18, sin sus dependencias.

## Instalación

```bash
pip install facturacion-electronica==0.24.0
```

Dependencias Odoo: `account`, `account_edi`, `stock`, `sale_stock`, `contacts`.

### En una base nueva

Odoo instala `l10n_cl` automáticamente si Contabilidad se instala en una
compañía chilena. Para evitarlo:

1. Crear la base sin datos de demostración y **sin país** (o con uno distinto de Chile).
2. Instalar `tf_dte_cl`, que instala Contabilidad con el plan genérico.
3. En la compañía: país **Chile**, RUT, dirección y moneda **CLP**. La moneda
   debe cambiarse antes de registrar cualquier asiento.
4. Instalar `tf_l10n_cl` y elegir **Chile (TF)** en
   *Facturación > Ajustes > Localización fiscal*. Si el paquete no aparece en
   el selector, se puede cargar desde la consola:
   `env['account.chart.template'].try_loading('cl_tf', env.company)`.
5. En Community, instalar `account_usability` (OCA).
6. Verificar en Aplicaciones que `l10n_cl` y los módulos `l10n_latam_*` **no**
   estén instalados.

### Catálogos incluidos

Se cargan al instalar o actualizar, solo con los registros que falten:

- **Comunas:** 346 comunas con su código y región (`hooks._tf_dte_cl_load_comunas`).
  Si una región no se empareja por nombre, la comuna queda sin región y se
  informa en el log.
- **Actividades económicas:** 674 actividades del SII (`data/sii_activities.csv`).
- **Tipos de documento de referencia:** catálogo oficial del formato DTE.

## Configuración

1. **Contabilidad > Ajustes > Facturación electrónica Chile**: ambiente
   (certificación por defecto), resolución, giro, comuna, correo DTE, actecos
   (hasta 4), unidad regional y certificado. El bloque muestra qué falta.
   La compañía debe usar moneda CLP y redondeo global de impuestos.
2. **Certificado digital**: "Cargar certificado" con el `.p12`/`.pfx`. El archivo
   y la contraseña no se guardan; la llave queda visible solo para
   administradores del sistema. Si un `.p12` antiguo (RC2-40-CBC) no abre, el
   servidor necesita el proveedor *legacy* de OpenSSL 3.
3. **CAF**: Contabilidad > Configuración > Facturación electrónica Chile > CAF.
   Se validan tipo, RUT, rango, solapamiento, llaves y codificación.
4. **Impuestos de venta**: con `tf_l10n_cl` ya vienen con su código SII. Si se
   crean a mano, asignar el código (14, 17, 18, 24, 25, 26, 27, 271): la tasa
   debe ser la oficial y el impuesto no puede estar incluido en el precio.
5. **Diarios de venta**: un diario por tipo (33, 34, 56, 61), con sucursal opcional.
6. **Tipos de operación** (entregas o traslados internos): tipo 52 e indicadores
   de traslado y despacho por defecto.
7. **Contactos**: RUT, giro, dirección, comuna y ciudad.

## Arquitectura

| Archivo | Responsabilidad |
|---|---|
| `models/sii_client.py` | Único punto de contacto con la librería. Neutraliza sus efectos globales (verificación TLS, codificador multipart, XML en el log), limpia los datos de entrada e interpreta las respuestas por código SII. |
| `models/dte_mixin.py` | Ciclo de vida común: validación, folio, firma, sobre, envío idempotente, consulta, reintento, restablecimiento y anulación de folios. |
| `models/dte_lines.py` | Reglas de impuestos SII, validación y armado del detalle, cálculo de totales. |
| `models/caf.py` | CAF, asignación de folios con bloqueo de fila y folios por anular. |
| `models/envelope.py` | Sobres EnvioDTE e intentos de envío. |
| `models/reference.py`, `models/document_type.py` | Referencias (zona E del formato DTE) y su catálogo oficial. |
| `models/ir_actions_report.py`, `report/` | Impresión: formato DTE en hoja y papel continuo, copia cedible y papel compacto para el reporte estándar de facturas. |
| `models/certificate.py` | Certificado PKCS#12. |
| `models/res_company.py`, `models/res_config_settings.py` | Configuración del emisor. |
| `models/account_move.py`, `models/account_edi_format.py` | Facturas y notas sobre `account_edi`. |
| `models/stock_picking.py` | Guías de despacho. |

### Ciclo de un DTE

1. **Al publicar o validar** (sin red): validación completa, folio, timbre y
   firma, sobre firmado. Si algo falla, la operación se revierte y el folio
   vuelve al CAF.
2. **Envío** (cron cada 5 minutos, y `account_edi` al publicar): se registra el
   intento en una transacción propia y se envía el sobre guardado. Si no hay
   respuesta, el documento queda "envío sin confirmar" y se consulta al SII
   antes de reenviar.
3. **Consulta** (cron cada 15 minutos): por Track ID (aceptado, con reparos o
   rechazado) o, sin Track ID, por documento.
4. Un documento rechazado se restablece con un folio nuevo (grupo
   "DTE: puede restablecer documentos electrónicos"); el folio anterior queda
   en "Folios por anular". Un folio firmado nunca se reutiliza en otro documento.

En Odoo 18 el cron de `account_edi` viene inactivo; por eso el módulo tiene su
propio cron de envío y mantiene sincronizado el documento EDI.

## Impresión

El formato sigue el *Manual de muestras impresas* del SII (versión 4.0). Los
botones **Imprimir**, **Descargar > PDF** y **Enviar** de las facturas usan el
formato DTE cuando el documento tiene folio; las facturas sin folio siguen con
el formato estándar de Odoo. Hay además una versión para papel continuo de 80 mm.

- Recuadro con RUT, tipo y folio (7,5 cm de ancho) y la unidad regional del SII debajo.
- Timbre de 9 × 4 cm como máximo, a más de 2 cm del borde izquierdo, con la
  leyenda de la resolución.
- Descuento por línea en monto, totales con la tasa de IVA.
- **Copia cedible** en facturas 33 y 34, y en guías que constituyen venta, con
  el acuse de recibo de la Ley 19.983. Se desactiva en Ajustes.

## Límites conocidos (facturacion_electronica 0.24.0)

- Referencias globales (`IndGlobal`) y RUT de otro contribuyente (`RUTOtr`):
  la librería no los transmite, por lo que están bloqueados.
- Una corrección de texto (`CodRef` 2) exige la razón en formato
  `DICE: ... DEBE DECIR: ...`.
- Un solo impuesto adicional por línea.
- Descuentos y recargos globales (`DscRcgGlobal`) no implementados: use el
  descuento por línea.

## Criterios propios (no definidos por el SII)

- La vigencia de un CAF es configurable (6 meses por defecto); confirme el plazo vigente.
- Una nota debe llevar `CodRef` en al menos una referencia a un documento tributario.
- Sin Track ID, un documento con estado `DOK` (o modificado por una nota) se da por aceptado.
- Guías valorizadas: precio de venta, precio de lista o 1 (con aviso en el chatter).
- Copia cedible de guías solo con indicador de traslado 1 (operación constituye venta).

## Estado y pendientes

Probado en producción:

- Factura electrónica (33) y nota de crédito (61) aceptadas por el SII.
- Representación impresa en hoja revisada contra el manual de muestras
  impresas: la factura con su copia tributaria y cedible (acuse de recibo y
  leyenda «CEDIBLE»), y la nota de crédito sin copia cedible, con su referencia
  al documento anulado.

Pendiente:

- Probar factura exenta (34), nota de débito (56) y guía de despacho (52),
  incluida su impresión.
- Revisar la impresión en papel continuo (80 mm) con documentos reales.
- Intercambio de DTE con el receptor.
- Suite de pruebas de Odoo (`tests/`).

## Licencia

LGPL-3. La librería `facturacion_electronica` se distribuye bajo GPLv3 o
posterior y no se incluye en este módulo; quien distribuya ambos en conjunto
debe cumplir los términos de la GPL.
