# tf_dte_cl — Facturación electrónica SII Chile (núcleo B2B)

Módulo técnico para Odoo 18 que emite documentos tributarios electrónicos (DTE)
ante el Servicio de Impuestos Internos (SII) de Chile usando la librería
[`facturacion_electronica`](https://gitlab.com/dansanti/facturacion_electronica) 0.24.0.

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

## Instalación

```bash
pip install facturacion-electronica==0.24.0
```

Dependencias Odoo: `account`, `account_edi`, `stock`, `sale_stock`, `contacts`.
El catálogo de 346 comunas se carga al instalar (`hooks._tf_dte_cl_load_comunas`);
si alguna región no se empareja por nombre, la comuna queda sin región y se
informa en el log del servidor.

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
4. **Impuestos de venta**: asignar el código SII (14, 17, 18, 24, 25, 26, 27,
   271). La tasa debe ser la oficial y el impuesto no puede estar incluido en el precio.
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

## Pendiente antes de producción

- Pruebas en el ambiente de certificación del SII y proceso de certificación.
- Revisar la representación impresa contra el Manual de muestras impresas vigente.
- Intercambio de DTE con el receptor.
- Suite de pruebas de Odoo (`tests/`).

## Licencia

LGPL-3. La librería `facturacion_electronica` se distribuye bajo GPLv3 o
posterior y no se incluye en este módulo; quien distribuya ambos en conjunto
debe cumplir los términos de la GPL.
