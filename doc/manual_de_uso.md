# Manual de uso — Facturación electrónica SII Chile (tf_dte_cl)

Módulo para Odoo 18, versión 18.0.2.0.0.

> **Estado de este manual.** Las secciones marcadas con 🔶 están pendientes de
> confirmar con envíos reales al SII y se completarán después de las pruebas en
> certificación.

---

## 1. Qué hace el módulo

Emite documentos tributarios electrónicos ante el Servicio de Impuestos Internos
y sigue su estado hasta la respuesta final.

| Documento | Código SII | Dónde se emite |
|---|---|---|
| Factura electrónica | 33 | Facturas de cliente |
| Factura no afecta o exenta electrónica | 34 | Facturas de cliente |
| Nota de débito electrónica | 56 | Facturas de cliente |
| Nota de crédito electrónica | 61 | Notas de crédito |
| Guía de despacho electrónica | 52 | Entregas y traslados internos |

**No incluye:** boletas electrónicas (39/41), punto de venta, facturas de compra
(46), liquidación-factura (43), documentos de exportación, libros electrónicos
ni cesión de facturas.

El **intercambio** con clientes y proveedores (envío del XML al cliente,
recepción de facturas de proveedores, aceptación y reclamo en el SII) está en el
módulo complementario `tf_dte_cl_intercambio`, que tiene su propio manual.

---

## 2. Conceptos básicos

- **Folio.** Número que identifica el documento ante el SII. Es distinto del
  número interno de Odoo: una factura puede ser `INV/2026/00012` en Odoo y tener
  el folio 45 en el SII.
- **CAF.** Archivo que el SII entrega con un rango de folios autorizados para un
  tipo de documento. Sin CAF vigente no se puede emitir.
- **Certificado digital.** Archivo `.p12` o `.pfx` con el que se firman los
  documentos. Tiene fecha de vencimiento.
- **Track ID.** Número que devuelve el SII al recibir un envío y que permite
  consultar su resultado.
- **Ambiente.** *Certificación* es el ambiente de pruebas del SII; *Producción*
  es el real. Mientras no esté certificado, todo se hace en certificación.

---

## 3. Configuración inicial

Se hace una sola vez por compañía. Requiere permisos de administrador contable.

### 3.1 Datos de la compañía

En **Ajustes > Compañías**, verifica:

- RUT correcto (el módulo valida el dígito verificador);
- dirección y ciudad;
- moneda **CLP**;
- redondeo de impuestos **global** (Contabilidad > Ajustes). El SII calcula el
  IVA sobre el total, no línea por línea.

### 3.2 Facturación electrónica

En **Contabilidad > Ajustes**, sección *Facturación electrónica Chile*:

| Campo | Qué poner |
|---|---|
| Ambiente SII | **Certificación** hasta que el SII autorice la emisión |
| N° de resolución | El de la resolución que autoriza a emitir. En certificación, 0 |
| Fecha de resolución | La de esa resolución |
| Unidad regional SII | Texto que se imprime bajo el folio, por ejemplo `S.I.I. - SANTIAGO CENTRO` |
| Giro | Giro comercial de la empresa |
| Comuna | Comuna de la casa matriz |
| Correo DTE | Casilla registrada en el SII |
| Actecos | Hasta 4 actividades económicas |
| Vigencia de CAF | Meses tras los cuales un CAF deja de usarse (6 por defecto) |
| Documento por defecto | 33 o 34 |
| Formato de impresión | A4 o térmico 80 mm |

Arriba del bloque aparece el **estado de la configuración**, que lista lo que
falta. Se actualiza al guardar.

### 3.3 Certificado digital

1. En el bloque *Certificado digital*, presiona **Cargar certificado**.
2. Adjunta el `.p12` o `.pfx` e ingresa su contraseña.
3. Presiona **Cargar**.

El módulo extrae el certificado, la llave y el RUT del firmante. **El archivo y
la contraseña no se guardan.** Si el certificado no informa el RUT del firmante,
complétalo a mano en su ficha.

El sistema avisa 30 días antes del vencimiento mediante una actividad dirigida
al usuario responsable.

> Si el certificado es antiguo y no abre, puede usar un cifrado que el servidor
> no tiene habilitado (RC2-40-CBC con OpenSSL 3). El mensaje lo indica; es un
> ajuste del servidor, no del certificado.

### 3.4 Cargar CAF

Descarga los CAF desde el sitio del SII (uno por tipo de documento) y cárgalos
en **Contabilidad > Configuración > Facturación electrónica Chile > CAF**:

1. **Nuevo**, adjunta el archivo y guarda.
2. El módulo completa tipo de documento, RUT, rango y fecha, y valida que el
   archivo corresponda a la compañía, que el rango no se solape con otro CAF y
   que las llaves sean coherentes.

El módulo reconoce si el CAF es de **certificación o de producción** y lo
muestra en la columna *Ambiente*. Al emitir, solo usa folios de CAF del ambiente
configurado en Ajustes: si cargas un CAF del otro ambiente, queda registrado con
un aviso, pero sus folios no se usan hasta que la compañía cambie de ambiente.
Por eso pueden convivir un CAF de certificación y uno de producción con los
mismos números de folio.

En la lista verás los folios disponibles y la fecha hasta la que se puede usar.
Cuando queden 10 folios o menos, la fila se muestra en naranja: es momento de
pedir un CAF nuevo.

### 3.5 Impuestos

En **Contabilidad > Configuración > Impuestos**, cada impuesto de venta usado en
DTE necesita su **Código SII**:

| Código | Impuesto | Tasa |
|---|---|---|
| 14 | IVA | 19 % |
| 17 | IVA anticipado faenamiento de carne | 5 % |
| 18 | IVA anticipado carne | 5 % |
| 24 | Licores, piscos, whisky y similares | 31,5 % |
| 25 | Vinos | 20,5 % |
| 26 | Cervezas y bebidas alcohólicas | 20,5 % |
| 27 | Bebidas analcohólicas y minerales | 10 % |
| 271 | Bebidas con elevado contenido de azúcares | 18 % |

Requisitos: la tasa debe ser la oficial y el impuesto **no puede estar incluido
en el precio** (los precios se informan netos).

### 3.6 Diarios de venta

Necesitas un diario por tipo de documento. En **Contabilidad > Configuración >
Diarios**, en cada diario de venta indica el **Tipo DTE** (33, 34, 56 o 61) y,
si corresponde, la **Sucursal SII**.

Las notas de crédito requieren su propio diario (61).

### 3.7 Tipos de operación para guías

En **Inventario > Configuración > Tipos de operación**, en las entregas (o
traslados internos) que emiten guía: **Tipo DTE** 52, la sucursal y, si quieres,
el indicador de traslado y el tipo de despacho por defecto.

### 3.8 Clientes

Cada cliente al que se le emitan DTE necesita: RUT válido, razón social, giro,
dirección, comuna y ciudad. El correo DTE es opcional pero recomendable.

Si el cliente factura a una dirección distinta, completa también la comuna en
esa dirección.

---

## 4. Uso diario

### 4.1 Emitir una factura

1. Crea la factura en el diario correspondiente (33 para afecta, 34 para exenta).
2. **Confirma**. En ese momento el módulo valida todo, toma el folio, timbra y
   firma el documento, sin conexión al SII.
3. El envío ocurre en segundo plano, dentro de los 5 minutos siguientes.
4. El estado se consulta automáticamente cada 15 minutos.

**Si algo falta**, la factura no se confirma y aparece el detalle de lo que hay
que corregir. El folio no se consume: puedes corregir esa factura o crear otra,
sin dejar huecos en la numeración del SII.

La pestaña **DTE** muestra folio, estado, Track ID y el XML firmado.

### 4.1.1 Descuentos y recargos globales

Para aplicar un descuento a toda la factura, agrega una línea con el producto
**Descuento global** y un **precio negativo** (por ejemplo, −2.201). Para un
recargo global (flete, embalaje), usa un producto marcado como *Descuento o
recargo global DTE* con precio positivo. Se pueden crear productos propios con
esa marca, en la ficha del producto.

- Con **IVA**, la línea descuenta o recarga el monto afecto; **sin impuestos**,
  el monto exento.
- El SII lo recibe como descuento o recargo global, no como una línea del
  detalle, y en el PDF aparece en los totales.
- No se admite en facturas con impuestos adicionales (ILA y similares): ahí
  aplica el descuento en cada línea.

### 4.2 Estados del DTE

| Estado | Qué significa | Qué hacer |
|---|---|---|
| Firmado, pendiente de envío | El documento está listo y espera el envío | Nada; el envío es automático |
| Enviado, en revisión del SII | El SII lo recibió y lo está procesando | Esperar; la respuesta suele demorar minutos 🔶 |
| Envío sin confirmar | No se supo si el envío llegó | Nada; el sistema consulta al SII y reenvía solo si corresponde |
| Aceptado | El SII lo aceptó | Nada |
| Aceptado con reparos | Aceptado, pero con observaciones | Revisar el detalle y corregir el origen para los próximos documentos |
| Rechazado | El SII lo rechazó | Ver la sección 4.6 |
| Error | Un problema impide firmar o enviar | Corregir lo indicado y presionar **Reintentar DTE** |

Los botones **Consultar SII**, **Reintentar DTE** y **Restablecer DTE** están en
la barra superior de la factura.

### 4.3 Nota de crédito

Desde una factura aceptada, usa **Agregar nota de crédito**. El módulo
selecciona el diario de notas de crédito y agrega la **referencia** al documento
original, indicando si lo anula o corrige montos.

Toda nota de crédito o débito debe referenciar el documento que modifica. Si
necesitas ajustar la referencia, hazlo en la pestaña **Referencias SII** antes
de confirmar.

Reglas que valida el módulo:

- una nota de crédito anula facturas o notas de débito; una nota de débito solo
  anula notas de crédito;
- la corrección de texto (código 2) es exclusiva de las notas de crédito, y la
  razón debe escribirse como `DICE: ... DEBE DECIR: ...` en una sola línea;
- una nota que anula o corrige texto debe tener una única referencia.

### 4.4 Guía de despacho

1. En la entrega, completa la pestaña **Guía de despacho**: indicador de
   traslado, tipo de despacho, transportista y patente si corresponde, y la
   dirección de destino (se propone la del cliente).
2. **Valida** la entrega. Ahí se firma la guía.
3. El envío y la consulta funcionan igual que en las facturas.

Las guías siempre se valorizan: se usa el precio de la línea de venta; si no
hay, el precio de lista del producto; y si tampoco, 1, dejando constancia en el
historial de la entrega.

### 4.5 Imprimir

Los botones **Imprimir**, **Descargar > PDF** y **Enviar** generan el documento en
formato SII cuando la factura tiene folio. Para papel de 80 mm usa
**Descargar > DTE (térmico)**. En las entregas, **Imprimir > Guía de despacho**.

El formato sigue el *Manual de muestras impresas* del SII (versión 4.0):

- recuadro con RUT, tipo de documento y folio, y bajo él la unidad regional
  («S.I.I. - ...», se completa en Ajustes);
- datos del emisor con casa matriz y sucursal, receptor, referencias y detalle
  con el descuento en monto;
- totales con la tasa de IVA; en documentos exentos solo monto exento y total;
- timbre electrónico de 9 × 4 cm como máximo, a más de 2 cm del borde izquierdo,
  con la leyenda de la resolución.

**Copia cedible.** Facturas (33 y 34) y guías que constituyen venta se imprimen
en dos páginas: la copia tributaria y la **cedible**, con el recuadro de acuse de
recibo (Ley 19.983) y la leyenda «CEDIBLE» (en guías, «CEDIBLE CON SU
FACTURA»). Las notas de crédito y débito, y las guías de traslado interno o que
no son venta, no llevan copia cedible. Se puede desactivar en Ajustes
(*Imprimir copia cedible*).

Las notas internas de la factura no se imprimen en el DTE: el manual no permite
textos con tratos comerciales en el documento tributario.

### 4.6 Documento rechazado

1. Abre el documento y revisa el detalle del rechazo en la pestaña DTE.
2. Corrige la causa (datos del cliente, montos, referencias).
3. Presiona **Restablecer DTE**, disponible solo para usuarios con el permiso
   correspondiente. El documento queda listo para emitirse con un **folio
   nuevo**, y el anterior pasa a *Folios por anular*.

### 4.7 Folios por anular

En **Contabilidad > Configuración > Facturación electrónica Chile > Folios por
anular** aparecen los folios que se firmaron pero nunca llegaron a ser válidos
ante el SII, por ejemplo por un rechazo o por un documento cancelado.

Debes anularlos en el sitio del SII y luego marcarlos como anulados con el botón
de la lista. 🔶 *El procedimiento exacto en el sitio del SII se documentará tras
las pruebas.*

### 4.8 Cancelar o modificar un documento emitido

- **Aceptado por el SII:** no se puede cancelar ni volver a borrador. Debe
  emitirse una nota de crédito.
- **Enviado y en revisión:** hay que esperar el resultado.
- **Firmado o en error, sin llegar al SII:** se puede volver a borrador o
  cancelar; el folio se registra para anulación.

---

## 5. Seguimiento y permisos

- **Sobres de envío SII** (menú de configuración) muestra cada envío con su
  Track ID, estado y los intentos realizados.
- El **permiso «DTE: puede restablecer documentos electrónicos»** se asigna en
  la ficha del usuario y habilita el botón Restablecer.
- Los certificados y sus llaves solo son visibles para administradores del
  sistema.

---

## 6. Problemas frecuentes

| Síntoma | Causa probable | Solución |
|---|---|---|
| «RUT inválido» al guardar un contacto | Dígito verificador que no corresponde | Verificar el RUT en el sitio del SII |
| «CAF 33 agotado» | Se acabaron los folios | Solicitar y cargar un CAF nuevo |
| «El CAF está vencido» | Pasó la vigencia configurada | Cargar un CAF nuevo y anular los folios no usados |
| «El impuesto no es válido para el SII» | Falta el código, la tasa no es la oficial o está incluido en el precio | Corregir la configuración del impuesto |
| «Los montos calculados para el DTE no coinciden» | Redondeo de impuestos por línea, o moneda distinta de CLP | Usar redondeo global y CLP |
| El documento queda en «Firmado» mucho rato | Los procesos automáticos están detenidos | Revisar que las tareas programadas estén activas |
| «Faltan datos del cliente» | Giro, comuna o dirección incompletos | Completar la ficha del cliente |

🔶 *Esta tabla se ampliará con los rechazos reales del SII y su explicación.*

---

## 7. Pendiente de documentar

- Puesta en producción: cambio de ambiente, resolución definitiva y CAF de producción.
- Tiempos reales de respuesta del SII y glosas de rechazo más comunes.
- Procedimiento de anulación de folios en el sitio del SII.
