# Manual de usuario — Facturación electrónica SII (TF)

Nov 3, 2026 · @César

## 1. Introducción

Este manual explica el uso diario de la facturación electrónica ante el Servicio de Impuestos Internos (SII) en Odoo 18. Está pensado para quienes emiten facturas, despachan mercadería, registran compras o preparan el F29. La instalación, la configuración inicial y la operación técnica están en el **Manual de administrador**.

| Módulo | Qué te permite hacer |
| --- | --- |
| Facturación electrónica (`tf_dte_cl`) | Emitir facturas (33), facturas exentas (34), notas de débito (56), notas de crédito (61) y guías de despacho (52) |
| Plan de cuentas Chile (`tf_l10n_cl`) | Trabajar con el plan de cuentas, impuestos y bancos de Chile |
| Intercambio (`tf_dte_cl_intercambio`) | Enviar los documentos a tus clientes y recibir, verificar, aceptar o reclamar los de tus proveedores |
| Cesión (`tf_dte_cl_cesion`) | Ceder facturas a un factoring |
| F29 (`tf_l10n_cl_f29`) | Calcular la declaración mensual y compararla con el Registro de Compras y Ventas |

**No incluye:** boletas electrónicas (39/41), punto de venta, facturas de compra (46), liquidación-factura (43), documentos de exportación ni libros electrónicos.

En todo el manual, las rutas de menú se escriben así: *Contabilidad > Configuración > Facturación electrónica Chile*.

## 2. Conceptos básicos

- **DTE.** Documento tributario electrónico: factura, nota o guía emitida en formato XML y firmada digitalmente.
- **Folio.** Número del documento ante el SII. Es distinto del número interno de Odoo: una factura puede ser `INV/2026/00012` en Odoo y tener el folio 45 en el SII. Odoo lo asigna solo al confirmar; nunca se escribe a mano.
- **CAF.** Archivo que entrega el SII con un rango de folios autorizados para un tipo de documento. Sin CAF vigente no se puede emitir.
- **Certificado digital.** Archivo con el que se firman los documentos. Tiene fecha de vencimiento y lo administra el administrador del sistema.
- **Track ID.** Número que devuelve el SII al recibir un envío; sirve para consultar su resultado.
- **Ambiente.** *Certificación* es el ambiente de pruebas del SII y *Producción* es el real. Un documento emitido en certificación no tiene validez tributaria.
- **Intercambio.** Envío del documento entre empresas (emisor y receptor), por correo, a la casilla que cada una registra en el SII.
- **Acuse de recibo.** Declaración del cliente de que recibió las mercaderías o servicios. Si no reclama en 8 días corridos, la ley lo presume.
- **Copia cedible.** Ejemplar impreso de la factura que permite cederla a un factoring.

### Cómo funciona la emisión

Al confirmar una factura, Odoo la valida, le asigna el folio, la timbra y la firma **sin conectarse al SII**. Después, procesos automáticos la envían (cada 5 minutos) y consultan su resultado (cada 15 minutos). No tienes que esperar con la factura abierta: puedes seguir trabajando.

## 3. Clientes, proveedores y productos

### 3.1 Datos del cliente

Para emitirle un DTE, el cliente necesita en su ficha:

| Campo | Comentario |
| --- | --- |
| **RUT** | Con dígito verificador correcto. Puedes escribirlo con puntos; Odoo lo guarda como `76086428-5` |
| **Razón social** | El nombre del contacto empresa |
| **Giro** | Glosa del giro comercial; en el DTE se informan hasta 40 caracteres |
| **Dirección, comuna y ciudad** | La comuna se elige de la lista oficial; al elegirla se completa la región |
| **Correo de intercambio DTE** | La casilla que el cliente registró en el SII. Si está vacío, no se le envía el documento por intercambio |

Si el RUT es inválido, Odoo no deja guardar el contacto. No se aceptan RUT genéricos (66666666-6) en facturas a empresas.

Si facturas a una dirección distinta de la principal, completa también la comuna en esa dirección. El giro y el correo de intercambio se comparten entre la empresa y sus contactos.

### 3.2 Proveedores

Los proveedores nuevos se crean solos al registrar un documento recibido, con el RUT, la razón social y el giro del XML. Revisa sus datos de pago y la cuenta contable después.

### 3.3 Transportistas y factoring

- Marca **Es transportista** en los contactos que trasladan mercadería: aparecen al llenar una guía de despacho.
- Marca **Factoring** en las empresas a las que cedes facturas: aparecen al ceder.

### 3.4 Productos

Los productos se facturan con sus impuestos de venta habituales (IVA 19 % u otro). Para vender sin IVA, quita el impuesto de la línea y usa el diario de facturas exentas (34).

El producto **Descuento global** sirve para aplicar descuentos a toda la factura (ver 4.3). Para crear un recargo global (flete, embalaje), crea un producto y márcalo como *Descuento o recargo global DTE*.

## 4. Emitir facturas y notas

### 4.1 Factura afecta (33) o exenta (34)

1. En *Contabilidad > Clientes > Facturas*, crea la factura y elige el **diario** que corresponde: facturas afectas (33) o exentas (34).
2. Completa cliente, fecha, plazo de pago y líneas.
3. **Confirma**. En ese momento Odoo valida todo, toma el folio, timbra y firma el documento.
4. El envío al SII y la consulta de su estado son automáticos.

**Si algo falta**, la factura no se confirma y aparece el detalle de lo que hay que corregir (por ejemplo, «falta giro» o «CAF 33 agotado»). El folio no se consume: corriges y vuelves a confirmar, sin dejar huecos en la numeración.

La pestaña **DTE** de la factura muestra el folio, el estado ante el SII, el Track ID y el XML firmado. El folio también aparece junto al título de la factura.

Reglas que conviene recordar:

- en una factura 33 cada línea afecta lleva IVA; en una 34 ninguna línea puede llevar IVA;
- los precios se ingresan **netos** (sin IVA);
- la factura debe estar en **pesos chilenos (CLP)**;
- la *forma de pago* (contado o crédito) depende del plazo de pago: un plazo distinto de «pago inmediato» se informa como crédito. Solo las facturas a crédito admiten aceptación, reclamo y cesión.

### 4.2 Impuestos adicionales

Los productos con ILA u otro impuesto adicional llevan el IVA más ese impuesto en la línea. Se admite **un solo impuesto adicional por línea**.

### 4.3 Descuentos y recargos globales

Para descontar sobre toda la factura, agrega una línea con el producto **Descuento global** y un **precio negativo** (por ejemplo, −2.201). Para un recargo global, usa un producto marcado como *Descuento o recargo global DTE* con precio positivo.

- Con **IVA**, la línea descuenta o recarga el monto afecto; **sin impuestos**, el exento.
- El SII lo recibe como descuento o recargo global, no como una línea del detalle; en el PDF aparece en los totales.
- No se admite en facturas con impuestos adicionales (ILA y similares): ahí aplica el descuento en cada línea.

Para un descuento solo en un producto, usa la columna **Desc. %** de esa línea.

### 4.4 Nota de crédito (61)

1. Desde la factura aceptada por el SII, usa **Agregar nota de crédito**.
2. Indica el motivo y si anula el documento completo o corrige montos.
3. Odoo elige el diario de notas de crédito y agrega la **referencia** a la factura original. Revísala en la pestaña **Referencias SII** antes de confirmar.
4. Confirma. El ciclo es el mismo de la factura.

### 4.5 Nota de débito (56)

Crea un documento en el diario de notas de débito y agrega en **Referencias SII** el documento que modifica. Una nota de débito sirve para aumentar el monto de una factura o para anular una nota de crédito.

### 4.6 Reglas de las referencias

Toda nota de crédito o débito debe referenciar el documento que modifica (tipo, folio, fecha, código y razón). Odoo valida:

| Código | Uso | Reglas |
| --- | --- | --- |
| 1 Anula documento | Anular por completo | Una NC anula facturas o ND; una ND solo anula NC. Una sola referencia |
| 2 Corrige texto | Corregir un dato sin cambiar montos | Solo en notas de crédito. Razón con el formato `DICE: ... DEBE DECIR: ...` en una línea. Una sola referencia |
| 3 Corrige montos | Ajustar montos | Puede haber varias referencias |

También puedes agregar referencias informativas a una factura (por ejemplo, la orden de compra del cliente, tipo 801) en la misma pestaña.

## 5. Seguimiento ante el SII

### 5.1 Estados del documento

| Estado | Qué significa | Qué hacer |
| --- | --- | --- |
| Firmado, pendiente de envío | Listo, espera el próximo envío | Nada; se envía en menos de 5 minutos |
| Enviado, en revisión del SII | El SII lo recibió y lo procesa | Esperar; suele demorar minutos |
| Envío sin confirmar | No se supo si el envío llegó | Nada; Odoo consulta al SII y reenvía solo si no llegó (espera 60 minutos) |
| **Aceptado** | El SII lo aceptó | Nada |
| Aceptado con reparos | Aceptado, con observaciones | Leer el detalle y corregir el origen para los próximos documentos |
| **Rechazado** | El SII lo rechazó | Ver 5.3 |
| Error | Un problema impide firmar o enviar | Corregir lo indicado y usar **Reintentar DTE** |

Botones de la barra superior: **Consultar SII** (actualiza el estado sin esperar), **Reintentar DTE** y **Restablecer DTE**.

En la lista de facturas, los filtros **DTE pendientes** y **DTE con problemas** muestran lo que requiere atención. Revísalos a diario.

### 5.2 Cancelar o modificar un documento emitido

- **Aceptado por el SII:** no se puede cancelar ni volver a borrador. Emite una nota de crédito.
- **Enviado y en revisión:** espera el resultado.
- **Firmado o con error, sin haber llegado al SII:** se puede volver a borrador o cancelar; el folio queda registrado para anularlo.

### 5.3 Documento rechazado

1. Abre el documento y lee el detalle del rechazo en la pestaña **DTE**.
2. Corrige la causa (datos del cliente, montos, referencias).
3. Usa **Restablecer DTE**. Solo lo ven los usuarios con el permiso *DTE: puede restablecer documentos electrónicos*.
4. Confirma de nuevo: el documento se emite con un **folio nuevo**, y el anterior pasa a *Folios por anular*.

### 5.4 Folios por anular

En *Contabilidad > Configuración > Facturación electrónica Chile > Folios por anular* están los folios que se firmaron pero nunca fueron válidos ante el SII (por rechazo o cancelación). Anúlalos en el sitio del SII y márcalos como anulados con el botón de la lista.

### 5.5 Sobres de envío

En *Sobres de envío SII* (mismo menú) ves cada envío con su Track ID, su estado y los intentos realizados. Es útil para saber cuándo se envió un documento.

## 6. Guías de despacho (52)

Las guías se emiten desde las **entregas** y los **traslados internos** de Inventario cuyo tipo de operación está configurado para emitir guía.

1. Abre la entrega y completa la pestaña **Guía de despacho**:
   - **Indicador de traslado**: 1 si la operación constituye venta, 5 para traslado interno, u otro que corresponda;
   - **Tipo de despacho**: quién traslada (emisor, cliente, etc.);
   - **Transportista**, chofer y **patente**, si corresponde;
   - **Dirección de destino**: se propone la del cliente; es obligatoria.
2. **Valida** la entrega. En ese momento se firma la guía.
3. El envío y la consulta al SII funcionan igual que en las facturas, con los mismos estados.

Las guías siempre se valorizan: se usa el precio de la línea de venta; si no hay, el precio de lista del producto; y si tampoco, 1, con una nota en el historial de la entrega.

Una guía no reemplaza a la factura: si la operación es una venta, después se emite la factura y puede referenciar la guía (tipo 52) en **Referencias SII**.

## 7. Impresión

| Qué | Cómo |
| --- | --- |
| Factura o nota en hoja carta/A4 | **Imprimir**, **Descargar > PDF** o **Enviar** desde la factura |
| Factura o nota en papel de 80 mm | **Descargar > DTE (térmico)** |
| Guía de despacho | En la entrega, **Imprimir > Guía de despacho** (o la versión térmica) |

Cuando el documento tiene folio, sale con el formato del SII (*Manual de muestras impresas*, versión 4.0): recuadro con RUT, tipo y folio; unidad regional del SII; datos del emisor y receptor; referencias; detalle; totales, y el **timbre electrónico** con la leyenda de la resolución. Las facturas sin folio (borradores) salen con el formato normal de Odoo.

**Copia cedible.** Las facturas 33 y 34, y las guías que constituyen venta, se imprimen en dos páginas: la copia tributaria y la **cedible**, con el recuadro de acuse de recibo (Ley 19.983) y la leyenda «CEDIBLE» (en guías, «CEDIBLE CON SU FACTURA»). Las notas y las guías de traslado interno no llevan copia cedible.

Las notas internas de la factura no se imprimen en el DTE.

El PDF térmico tiene altura fija, así que en pantalla queda espacio en blanco al final; la impresora corta según el contenido.

## 8. Intercambio con clientes

### 8.1 Envío al cliente

No tienes que hacer nada. Cuando el SII acepta una factura, nota o guía, un proceso automático envía al **correo de intercambio** del cliente, dentro de 15 minutos, el **XML** (en un sobre con su RUT) y el **PDF**.

En la pestaña **DTE**, el bloque *Intercambio con el receptor* muestra el estado, el correo usado y la fecha. El botón **Enviar al receptor** lo envía a mano, por ejemplo después de completar un correo que faltaba; no reenvía un documento ya enviado.

### 8.2 Respuesta del cliente

En las **facturas 33 y 34 a crédito**, el cliente tiene **8 días corridos** para aceptarlas o reclamarlas en el SII. Odoo lo consulta cada 4 horas y lo muestra en el bloque *Respuesta del cliente*:

| Estado | Significado |
| --- | --- |
| Pendiente | Sin respuesta, dentro del plazo |
| Aceptado por el cliente | Aceptó el contenido |
| Acuse de recibo otorgado | Declaró recibidas las mercaderías o servicios |
| **Reclamado por el cliente** | Reclamó el contenido o la falta de mercaderías |
| Acuse de recibo presunto | Pasó el plazo sin reclamo |
| No aplica | Factura al contado, nota o guía |

**Si un cliente reclama**, queda un mensaje en la factura y una **actividad para el vendedor**. Revisa el caso y, si procede, emite una nota de crédito. El filtro **Reclamadas por el cliente** las reúne.

El botón **Consultar eventos del cliente** actualiza el estado sin esperar.

Algunos clientes responden además por correo con un XML; Odoo lo registra y lo informa en la factura. Esas respuestas son informativas: lo que vale legalmente es lo registrado en el SII.

## 9. Documentos de proveedores

### 9.1 Cómo llegan

- **Por correo**, a la casilla de intercambio de la empresa. Se descargan solos y aparecen en *Contabilidad > Configuración > Facturación electrónica Chile > Documentos recibidos*.
- **A mano**, con **Cargar XML de proveedor**, para documentos descargados del portal de un proveedor.

Cada correo queda como un **sobre recibido** y cada documento que trae, como un **documento recibido**. Odoo responde solo al proveedor con la *respuesta de recepción*, un acuse técnico que no implica aceptar la factura.

### 9.2 Verificación en el SII

Cada documento recibido se consulta en el SII para confirmar que existe y que sus datos coinciden:

| Resultado | Factura de proveedor |
| --- | --- |
| **Verificado en el SII** | Se crea normalmente |
| Datos no coinciden o modificado por una nota | Se crea con aviso: revísala antes de confirmar |
| **No recibido por el SII** | Bloqueada. Puede que el proveedor aún no lo envíe; se sigue consultando durante 10 días |
| Emisor no autorizado o anulado | Bloqueada |

La verificación es automática cada 30 minutos; **Verificar en el SII** la hace a mano.

### 9.3 Crear la factura de proveedor

Desde el documento recibido, usa **Crear factura de proveedor**. Queda un **borrador** con el proveedor, la fecha, una línea por cada detalle del XML, los descuentos y recargos globales, el IVA de compra y el XML adjunto. También trae el **Tipo de documento SII (compra)** y el **Folio del proveedor**, que usa el F29.

Revisa las cuentas, el diario y el total (si no coincide, queda un aviso en el historial) y confirma.

- Una **nota de crédito** de un proveedor queda enlazada a la factura que corrige, si esa factura ya está registrada.
- Si un documento no corresponde facturarlo, usa **Descartar**.

> **Importante:** no uses el botón **Subir** de la lista de facturas de proveedor. Es una función estándar de Odoo que no lee el DTE chileno y deja la factura en blanco. Los documentos deben entrar por *Documentos recibidos*.

### 9.4 Aceptar o reclamar en el SII

Las **facturas 33 y 34 a crédito** recibidas se aceptan o reclaman dentro de **8 días corridos** desde su recepción en el SII. Pasado el plazo, se presume el acuse de recibo y ya no se puede reclamar.

| Botón | Qué registra en el SII |
| --- | --- |
| **Aceptar en el SII** | Acepta el contenido y otorga el acuse de recibo |
| **Reclamar en el SII** | Reclamo al contenido, por falta parcial o total de mercaderías. El motivo queda en el historial |
| **Consultar SII** | Trae los eventos registrados, sin cambiar nada |

Un documento reclamado no se puede aceptar, y uno aceptado no se puede reclamar. Las facturas **al contado** no admiten aceptación ni reclamo: no muestran los botones.

El campo *Recepción en el SII* muestra la fecha desde la que corre el plazo; si aún no se conoce, se estima desde la emisión y se marca **(estimado)**. El filtro **Sin respuesta SII** muestra los pendientes, y los vencidos aparecen en rojo.

### 9.5 Avisos de plazo

Cuando a una factura a crédito le quedan 2 días (o lo configurado) sin respuesta, Odoo crea una **actividad** que vence el mismo día del plazo. Se asigna a quien creó la factura de proveedor; si no existe, al **responsable de documentos recibidos**. Se cierra sola al aceptar o reclamar.

### 9.6 Guías de despacho recibidas

Las guías (52) no generan factura, pero se asocian a la **recepción de inventario**:

- si la guía referencia una **orden de compra**, a la recepción de esa orden;
- si no, a la recepción del mismo proveedor con fecha cercana (hasta 7 días), solo si hay **una única candidata**.

Si no hay coincidencia segura, usa **Buscar** junto al campo *Recepción* o elígela a mano. La recepción muestra sus guías en la pestaña **Guías del proveedor**.

## 10. Cesión de facturas (factoring)

Ceder una factura es transferir a otra empresa, normalmente un factoring, el derecho a cobrarla. Se hace con un **Archivo Electrónico de Cesión (AEC)** firmado y anotado en el Registro de Transferencia de Créditos del SII. **Una vez anotada, la cesión no se puede anular.**

### 10.1 Requisitos

Odoo los valida antes de firmar:

- factura 33 o 34 **aceptada por el SII**;
- **a crédito** (una factura al contado no se puede ceder);
- con **acuse de recibo** del cliente, otorgado o presunto (8 días sin reclamo); lo ves en *Respuesta del cliente*;
- **no reclamada** y sin otra cesión.

### 10.2 Ceder

1. En la factura, usa **Ceder factura**.
2. Elige el **factoring** (contactos marcados como *Factoring*) o completa sus datos: RUT, razón social, dirección y correo. Si es nuevo, queda guardado.
3. Revisa el **monto cedido** (por defecto, el total) y el **vencimiento**.
4. Usa **Firmar cesión**. Si falta un requisito, Odoo lo informa y no firma.

El envío al SII es automático (cada 15 minutos). La factura muestra el estado de la cesión y el cesionario en la pestaña DTE, y la lista de facturas tiene el filtro **Cedidas**. El AEC firmado queda en la cesión, pestaña **AEC**, por si el factoring lo pide.

### 10.3 Estados

En *Facturación electrónica Chile > Cesiones de facturas*:

| Estado | Qué hacer |
| --- | --- |
| Firmada / Enviada al SII | Nada; el proceso automático la envía y la consulta |
| Envío sin confirmar | Nada; se consulta antes de reenviar, para no duplicar |
| **Cedida** | El SII anotó la cesión. Si aparece *Confirmada por el SII*, está registrada aunque el SII no informe el tenedor |
| Rechazada | Lee la respuesta del SII, corrige y usa **Firmar AEC** otra vez |
| Con error | Lee el mensaje (por ejemplo, del certificado) y vuelve a firmar |

Una cesión se puede **cancelar** solo mientras no se haya enviado al SII.

## 11. Declaración mensual F29

Odoo **calcula** los códigos del F29 desde las facturas de venta y de proveedor y los **compara** con el Registro de Compras y Ventas (RCV) del SII. No presenta la declaración: la completas en el sitio del SII con los valores propuestos.

### 11.1 Antes de empezar

- Las facturas de proveedor deben tener **Tipo de documento SII (compra)** y **Folio del proveedor**. Las creadas desde *Documentos recibidos* ya los traen; en las demás, complétalos.
- Las **boletas de honorarios** se registran como *Boleta de honorarios electrónica*, con el impuesto de retención.

### 11.2 Calcular

1. *Contabilidad > Informes > Declaraciones F29*, **Nuevo**.
2. Elige **mes y año**. Revisa la **tasa de PPM** (se propone la del mes anterior) y el **crédito PPV**, si corresponde.
3. **Remanente:** si el F29 del mes anterior está en Odoo, se toma solo. Ingresa la **UTM** del mes anterior y la del período para reajustarlo; si no, se usa el monto nominal y la declaración lo advierte.
4. Usa **Calcular**. Cada código muestra su valor, y el ícono de lista abre los documentos que lo forman.

| Sección | Códigos |
| --- | --- |
| Ventas exentas | 586, 142 |
| Débitos | 503/502, 512/513, 509/510, 538 |
| Compras sin crédito | 564/521, 584/562 |
| Créditos | 519/520, 761/762, 524/525, 527/528, 531/532, 504, 537 |
| Resultado | 89 (IVA determinado) o 77 (remanente) |
| Renta | 151 (retención de honorarios, según fecha de pago), 563/115/68/62 (PPM) |
| Total | 595, 91 |

Lee los **avisos** de la parte superior: indican lo que el cálculo no incluye y debes completar a mano. Odoo no calcula IVA de uso común (proporcionalidad), impuestos adicionales (ILA), combustibles, cambio de sujeto ni exportaciones.

### 11.3 Comparar con el RCV

1. En el sitio del SII, *Registro de Compras y Ventas*, elige el período y descarga el **detalle** de ventas y el de compras.
2. En la declaración, usa **Comparar con el RCV** y carga los archivos tal como se descargaron.
3. La pestaña **Comparación con el RCV** muestra cada diferencia:

| Resultado | Qué hacer |
| --- | --- |
| Solo en el SII | Falta registrarlo en Odoo (típicamente una factura de proveedor) |
| Solo en Odoo | Revisa tipo, folio o RUT; o el SII aún no lo registra |
| Montos distintos | Compara la factura en Odoo con el documento |

En la pestaña **Códigos**, las columnas *RCV del SII* y *Diferencia* muestran cada código según el RCV; los que difieren aparecen en rojo.

### 11.4 Presentar

Completa el F29 en el sitio del SII y luego usa **Marcar como presentada** para bloquear la declaración. Su código 77 será el remanente del mes siguiente.

## 12. Preguntas frecuentes

| Situación | Causa probable | Qué hacer |
| --- | --- | --- |
| «RUT inválido» al guardar un contacto | Dígito verificador incorrecto | Verificar el RUT en el sitio del SII |
| La factura no se confirma: «Faltan datos del cliente» | Giro, comuna, dirección o ciudad vacíos | Completar la ficha del cliente |
| «CAF 33 agotado» o «El CAF está vencido» | No quedan folios válidos | Avisar al administrador para cargar un CAF nuevo |
| «El impuesto no es válido para el SII» | Impuesto sin código SII, con tasa no oficial o incluido en el precio | Avisar al administrador |
| «Los montos calculados no coinciden» | Moneda distinta de CLP o redondeo por línea | Facturar en CLP; avisar al administrador si persiste |
| El documento sigue en «Firmado» después de 30 minutos | Procesos automáticos detenidos | Usar **Consultar SII**; si no cambia, avisar al administrador |
| No puedo cancelar una factura | Ya fue aceptada por el SII | Emitir una nota de crédito |
| No veo **Restablecer DTE** | Falta el permiso | Pedirlo al administrador |
| El cliente no recibió el XML | Sin correo de intercambio en su ficha | Completar el correo y usar **Enviar al receptor** |
| La factura de proveedor salió en blanco | Se usó el botón **Subir** | Crearla desde *Documentos recibidos* |
| «No recibido por el SII» en un documento de proveedor | El proveedor aún no lo envía al SII | Esperar la siguiente verificación; si sigue, consultar al proveedor |
| No aparecen los botones Aceptar o Reclamar | Factura al contado, nota o guía | Es lo esperado |
| El SII responde «fuera de plazo» | Pasaron los 8 días | El acuse de recibo ya se presume |
| No puedo ceder una factura | Es al contado, está reclamada o aún sin acuse de recibo | Esperar el acuse (o los 8 días) |
| El F29 no cuadra con el RCV | Documentos faltantes o con tipo/folio distinto | Revisar la pestaña *Comparación con el RCV* |
