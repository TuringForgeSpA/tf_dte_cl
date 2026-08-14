# -*- coding: utf-8 -*-
import base64
import collections
import logging
from io import BytesIO

from PIL import Image

from odoo import _, models
from odoo.exceptions import UserError

# La librería de terceros ya vive dentro del propio módulo (ver
# account_move.py original: "from .facturacion_electronica import
# facturacion_electronica as fe"). Se importa igual aquí.
from .facturacion_electronica import facturacion_electronica as fe

try:
    import requests
except ImportError:  # pragma: no cover - requests viene con Odoo, esto es solo defensivo
    requests = None

_logger = logging.getLogger(__name__)

# Código técnico único de este formato. Debe coincidir EXACTO con el
# <field name="code">sii_dte</field> del registro en data/account_edi_data.xml
DTE_CODE = 'sii_dte'

# cod_dte (código de documento SII) que hoy vive en account.journal.
# Migrados desde el filtro que existía a mano en AccountMove._post().
DTE_CODES_FACTURACION = ['33', '34', '43', '46', '52', '56', '61']  # Facturas, NC, ND, Guías, etc.
DTE_CODES_BOLETA = ['39', '41']  # Boletas: timbraje + envío en un solo paso (flujo distinto en la librería fe)
DTE_CODES_SOPORTADOS = DTE_CODES_FACTURACION + DTE_CODES_BOLETA


class SiiEdiError(Exception):
    """Excepción interna: transporta un error del SII ya clasificado
    (mensaje legible + severidad) desde las llamadas a la librería
    `fe` hasta `_post_invoice_edi`, listo para convertirse en el dict
    que el framework EDI de Odoo espera para pintar el banner de la
    factura, sin perder el detalle técnico en el log.
    """

    def __init__(self, message, blocking_level='error'):
        super().__init__(message)
        self.message = message
        self.blocking_level = blocking_level

    def to_edi_result(self):
        return {'error': self.message, 'blocking_level': self.blocking_level}


def _classify_sii_exception(error):
    """Traduce una excepción cruda (de la librería facturacion_electronica
    o de la capa HTTP que usa por debajo) a un mensaje legible + un
    blocking_level para el banner de la factura.

    Criterio:
      - HTTP 4xx (schema, firma, RUT receptor, etc.): reenviar el MISMO
        XML volverá a fallar -> 'error' (requiere corregir algo antes de
        reintentar manualmente).
      - HTTP 5xx / timeout / caída de conexión: problema transitorio del
        SII o de la red -> 'warning' (el cron de reintentos de
        account.edi.document puede resolverlo solo).
      - Cualquier otra excepción de la librería (CAF agotado, firma
        inválida, etc.): se trata como bloqueante por defecto.
    """
    status_code = getattr(error, 'status_code', None)
    response = getattr(error, 'response', None)
    body = None
    if response is not None:
        status_code = status_code or getattr(response, 'status_code', None)
        body = getattr(response, 'text', None)

    if status_code and 400 <= status_code < 500:
        message = _("El SII rechazó el envío por un error de esquema o validación (HTTP %(code)s).") % {
            'code': status_code,
        }
        if body:
            message += "\n%s" % body
        return message, 'error'

    if status_code and status_code >= 500:
        message = _(
            "El servicio del SII no está disponible en este momento (HTTP %(code)s). "
            "Se reintentará automáticamente."
        ) % {'code': status_code}
        return message, 'warning'

    if requests is not None and isinstance(error, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return _("No fue posible conectar con el SII (%s). Se reintentará automáticamente.") % error, 'warning'

    return _("El SII rechazó el DTE: %s") % error, 'error'


class AccountEdiFormat(models.Model):
    """Extiende el modelo genérico account.edi.format (NO se crea un modelo
    nuevo) para que sepa emitir/consultar Documentos Tributarios
    Electrónicos ante el SII de Chile.

    Toda la lógica que hoy está acoplada en account.move (data_emisor,
    receptor, detalle_doc, referencias, enviar_dte, consulta_estado_dte,
    etc.) debe terminar migrada aquí como métodos privados _l10n_cl_*,
    recibiendo `move` como parámetro en vez de usar `self` como la factura.
    """
    _inherit = 'account.edi.format'

    # ==========================================================
    # 1. APLICABILIDAD Y COMPATIBILIDAD
    # ==========================================================

    def _get_move_applicability(self, move):
        """Punto de entrada central del framework: por cada
        account.edi.format instalado, Odoo llama a este método para
        cada account.move y le pregunta "¿te haces cargo de este
        documento?". Si la respuesta es un dict, el framework usa esas
        funciones para post/cancel/generar contenido; si es False/{}, este
        formato se ignora para ese move.

        Sustituye al chequeo manual que hoy vive en account.move._post():
            cod_dte_lst = ['33','34','39','41','43','46','52','56','61']
            if not cod_dte or (cod_dte not in cod_dte_lst): ...
        """
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._get_move_applicability(move)

        if not self._l10n_cl_is_dte_applicable(move):
            return False

        return {
            'post': self._post_invoice_edi,
            'cancel': self._cancel_invoice_edi,
            'edi_content': self._get_invoice_edi_content,
        }

    def _l10n_cl_is_dte_applicable(self, move):
        """Regla de negocio pura: ¿este move debería generar un DTE?
        Debe considerar tipo de movimiento, país/compañía y que el
        diario tenga configurado un cod_dte soportado (hoy en
        journal_id.cod_dte / journal_id.config_dte_id).
        """
        self.ensure_one()
        if move.move_type not in ('out_invoice', 'out_refund'):
            return False
        if move.country_code != 'CL':
            return False
        cod_dte = move.journal_id.cod_dte
        return bool(cod_dte) and cod_dte in DTE_CODES_SOPORTADOS

    def _is_compatible_with_journal(self, journal):
        """Controla si este formato aparece seleccionable en la
        configuración "Electronic Invoicing" del diario. Reemplaza el
        dominio armado a mano hoy en _compute_suitable_journal_ids /
        _get_default_journal.
        """
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._is_compatible_with_journal(journal)
        return journal.type == 'sale' and journal.country_code == 'CL' and bool(journal.cod_dte)

    def _needs_web_services(self):
        """True: el envío no es "generar un XML y listo", implica
        timbrar y transmitir contra los webservices del SII (a través de
        la librería facturacion_electronica). Esto hace que el framework
        use la cola asíncrona de account.edi.document (to_send -> sent)
        en vez de adjuntar el XML de forma síncrona al confirmar.
        """
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._needs_web_services()
        return True

    def _is_required_for_invoice(self, invoice):
        """Si el diario tiene cod_dte configurado, el DTE es obligatorio:
        la factura no debería considerarse completamente procesada hasta
        que el envío tenga éxito. Sustituye el bloqueo manual que hoy
        aplican button_draft()/unlink() al revisar estado_dte.
        """
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._is_required_for_invoice(invoice)
        return self._l10n_cl_is_dte_applicable(invoice)

    def _support_batching(self, move=None, state=None, company=None):
        """Por ahora se envía documento a documento, igual que hoy. Se
        deja declarado porque el envío masivo de boletas (RCOF/consumo
        de folios) sí admite batching a futuro, agrupando por diario +
        fecha de emisión.
        """
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._support_batching(move=move, state=state, company=company)
        return False

    # ==========================================================
    # 2. VALIDACIÓN PREVIA AL ENVÍO
    # ==========================================================

    def _check_move_configuration(self, move):
        """Se ejecuta ANTES de intentar enviar. Debe devolver una lista
        de strings con errores de configuración; si no está vacía, Odoo
        bloquea el envío y se los muestra al usuario sin llegar a llamar
        a _post_invoice_edi.

        Aquí deben migrarse (adaptadas para devolver strings en vez de
        lanzar UserError):
          - actecos(conf)        -> la compañía debe tener al menos un Acteco
          - revisar_cliente(...) -> el receptor debe tener vat/dirección/
                                     comuna/giro/contacto completos
          - caf_file / verifica_folio -> debe existir un CAF vigente para
                                     el cod_dte y el folio de move.name debe
                                     caer dentro del rango del CAF
        """
        self.ensure_one()
        errors = super()._check_move_configuration(move)
        if self.code != DTE_CODE:
            return errors

        conf = move.journal_id.config_dte_id
        if not conf:
            errors.append(_("El diario '%s' no tiene una configuración DTE asociada (config_dte_id).") % move.journal_id.name)
            return errors

        # TODO: migrar aquí las validaciones de actecos / revisar_cliente / verifica_folio
        # errors += self._l10n_cl_check_actecos(conf)
        # errors += self._l10n_cl_check_receptor(move.partner_id)
        # errors += self._l10n_cl_check_caf(conf, move)

        return errors

    # ==========================================================
    # 3. ENVÍO
    # ==========================================================

    def _post_invoice_edi(self, invoices):
        """Método de envío real. Sustituye a enviar_dte() / crear_dte() /
        timbrar_dte() de account.move.

        Contrato exigido por el framework: recibe un recordset de moves y
        devuelve un dict {move: {...}}, por cada uno:
          - éxito -> {'success': True, 'attachment': <ir.attachment>}
          - error -> {'error': '<mensaje o html>', 'blocking_level': 'error'|'warning'}

        El framework se encarga de crear/actualizar el account.edi.document,
        adjuntar el attachment al move y mover el estado de 'to_send' a
        'sent' (o dejarlo marcado en error).
        """
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._post_invoice_edi(invoices)

        result = {}
        for move in invoices:
            cod_dte = move.journal_id.cod_dte
            conf = move.journal_id.config_dte_id
            try:
                # Todo lo que toca la BD para ESTE move (adjunto, xml.envio,
                # write de track_id/estado) queda dentro del savepoint. Si
                # algo falla a mitad de camino -sea la llamada al SII o un
                # error al grabar el resultado- solo se deshace lo de este
                # move: el resto del lote y la transacción que hizo
                # _post_invoice_edi (p. ej. la validación contable) siguen
                # intactos.
                with self.env.cr.savepoint():
                    response = self._l10n_cl_send_dte(move, conf, cod_dte)
                    attachment = self._l10n_cl_create_dte_attachment(move, response, cod_dte)
                    self._l10n_cl_process_send_response(move, response, cod_dte)
                result[move] = {
                    'success': True,
                    'attachment': attachment,
                }
            except SiiEdiError as error:
                # Error ya clasificado (schema/validación/HTTP/conexión):
                # va directo al banner con la severidad correcta.
                result[move] = error.to_edi_result()
            except UserError as error:
                result[move] = {
                    'error': str(error),
                    'blocking_level': 'error',
                }
            except Exception:
                _logger.exception("Error inesperado enviando el DTE al SII para %s", move.name)
                result[move] = {
                    'error': _("Error inesperado al enviar el DTE al SII. Revise el log del servidor."),
                    'blocking_level': 'error',
                }
        return result

    def _cancel_invoice_edi(self, invoices):
        """Cancelación EDI. OJO: en Chile un DTE ya ACEPTADO por el SII no
        se anula por este mecanismo, se anula emitiendo una Nota de
        Crédito referenciada (cod_dte 61). Este método solo debería
        permitir descartar documentos que aún están en 'to_send' (todavía
        no timbrados/enviados); si ya fueron aceptados, debe bloquear y
        guiar al usuario a emitir la NC.
        """
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._cancel_invoice_edi(invoices)

        result = {}
        for move in invoices:
            if move.estado_dte in ('Aceptado', 'Reparo'):
                result[move] = {
                    'error': _(
                        "Este DTE ya fue aceptado por el SII. Para anularlo debe emitir una "
                        "Nota de Crédito referenciada (código 61); no puede cancelarse el envío EDI."
                    ),
                    'blocking_level': 'error',
                }
            else:
                result[move] = {'success': True}
        return result

    # ==========================================================
    # 4. ACTUALIZACIÓN DE ESTADO (TRACK ID)
    # ==========================================================

    def _l10n_cl_cron_update_dte_status(self):
        """Método pensado para ser llamado por un ir.cron periódico (a
        declarar en data/ir_cron_data.xml). Sustituye la llamada manual
        a consulta_estado_dte() / consulta_estado_documento().

        Recorre los account.edi.document en estado 'sent' de este
        formato cuyo move todavía no tiene una respuesta definitiva del
        SII (Aceptado/Rechazado) y consulta su Track ID.
        """
        edi_format = self.search([('code', '=', DTE_CODE)], limit=1)
        if not edi_format:
            return

        documents = self.env['account.edi.document'].search([
            ('edi_format_id', '=', edi_format.id),
            ('state', '=', 'sent'),
            ('move_id.track_id', '!=', False),
            ('move_id.estado_dte', 'not in', ['Aceptado', 'Rechazado']),
        ])
        for document in documents:
            edi_format._l10n_cl_update_dte_status(document)

    def _l10n_cl_update_dte_status(self, document):
        """Consulta el estado de UN account.edi.document puntual contra
        el SII (usando move.track_id) y traduce la respuesta al
        vocabulario del framework EDI:

          - Aceptado          -> el document se mantiene 'sent', sin error
          - Reparo            -> document.error se llena + blocking_level='warning'
          - Rechazado         -> document.error se llena + blocking_level='error'
          - Sigue en proceso  -> no se toca nada, se reintenta en el próximo cron

        Sustituye a consulta_estado_dte() / procesar_respuesta_xml() /
        procesar_respuesta_boleta() de account.move.
        """
        self.ensure_one()
        move = document.move_id
        cod_dte = move.journal_id.cod_dte

        # TODO: migrar aquí la llamada real:
        #   data = {'Emisor': ..., 'firma_electronica': ..., 'codigo_envio': move.track_id}
        #   if cod_dte in DTE_CODES_BOLETA:
        #       response = fe.consulta_estado_documento(data)
        #   else:
        #       response = fe.consulta_estado_dte(data)
        #   estado, glosa = self._l10n_cl_parse_status_response(response, cod_dte)
        estado, glosa = self._l10n_cl_query_dte_status(move, cod_dte)

        move.write({
            'estado_dte': estado,
            'detalle_estado': glosa,
        })

        if estado == 'Rechazado':
            document.write({'error': glosa, 'blocking_level': 'error'})
        elif estado == 'Reparo':
            document.write({'error': glosa, 'blocking_level': 'warning'})
        elif estado == 'Aceptado':
            document.write({'error': False, 'blocking_level': False})
        # si sigue "EnProceso" no se modifica el account.edi.document: se reintenta después

    # ==========================================================
    # 5. CONTENIDO / NOMBRE DE ARCHIVO
    # ==========================================================

    def _get_invoice_edi_content(self, move):
        """Devuelve el XML del DTE en bytes, usado para previsualización
        o descarga manual sin pasar por el flujo de envío completo.
        """
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._get_invoice_edi_content(move)
        # TODO: si ya existe un adjunto generado (envío previo), devolverlo.
        # Si no, construir el payload sin timbrar solo para inspección.
        return b""

    def _get_invoice_edi_filename(self, move):
        """Nombre del archivo adjunto. Reemplaza la convención
        'T<tipo>F<folio>' usada hoy en crear_xml_envio_boleta/timbrar_dte.
        """
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._get_invoice_edi_filename(move)
        cod_dte = move.journal_id.cod_dte or ''
        folio = move.folio(move.name)
        return f"DTE_T{cod_dte}F{folio}.xml"

    # ==========================================================
    # 6. HELPERS INTERNOS
    #    (a completar migrando línea a línea la lógica de account.move)
    # ==========================================================

    def _l10n_cl_send_dte(self, move, conf, cod_dte):
        """Arma el payload (Emisor/Receptor/Detalle/Referencias/Totales)
        y llama a la librería facturacion_electronica.

        Reutiliza, tal cual, los builders que hoy viven en account.move
        (data_emisor, data_documento, data_firma_electronica, api_dte)
        para no tener que migrar toda esa lógica en el mismo paso; sólo
        se movió el "orquestador" (quién arma el dict final y quién
        decide timbrar vs. timbrar-y-enviar) hasta acá.

        Lanza SiiEdiError con el mensaje/severidad ya listos para el
        banner si la librería `fe` falla (schema, HTTP 4xx/5xx, firma,
        conexión, etc.).
        """
        self.ensure_one()

        data = collections.OrderedDict()
        data['Emisor'] = move.data_emisor(conf)
        # Tal cual el código original: este RUT queda fijo, no es el RUT
        # del receptor real de la factura. Se preserva el valor exacto;
        # confirmar con el equipo si en algún caso debe ser dinámico.
        data['RutReceptor'] = '60803000-K'
        data['firma_electronica'] = move.data_firma_electronica(conf)
        data['Documento'] = move.data_documento(move, conf, cod_dte)
        data['api'] = move.api_dte(cod_dte)

        try:
            if cod_dte in DTE_CODES_BOLETA:
                # Boleta: timbra y envía en la misma llamada.
                response = fe.timbrar_y_enviar(data)
            else:
                # Factura/NC/Guía/etc.: solo timbra. El envío del sobre al
                # SII (y su Track ID) queda para un paso posterior, igual
                # que en timbrar_dte() del código original.
                response = fe.timbrar(data)
        except SiiEdiError:
            raise
        except Exception as error:
            message, blocking_level = _classify_sii_exception(error)
            _logger.warning("SII rechazó/falló el envío de %s: %s", move.name, error)
            raise SiiEdiError(message, blocking_level) from error

        if not response:
            raise SiiEdiError(
                _("El SII no devolvió una respuesta al intentar timbrar/enviar el DTE."),
                'warning',
            )
        return response

    def _l10n_cl_query_dte_status(self, move, cod_dte):
        """Llama a fe.consulta_estado_dte / fe.consulta_estado_documento
        y devuelve una tupla (estado, glosa) normalizada.

        NOTA: no se implementa en este paso (ver _l10n_cl_update_dte_status
        más arriba); sigue declarado como TODO a propósito para no mezclar
        la consulta de estado con el envío, que es lo que se pidió resolver
        ahora.
        """
        self.ensure_one()
        raise NotImplementedError(_("Falta migrar la consulta de estado del DTE."))

    def _l10n_cl_create_dte_attachment(self, move, response, cod_dte):
        """Crea el ir.attachment estándar (lo que el framework EDI espera
        devolver en 'attachment') y, en paralelo, un registro `xml.envio`
        que ya NO es la fuente de verdad del estado EDI (eso lo hace
        account.edi.document), pero se conserva como registro de
        auditoría/impresión: guarda el código de barras PDF417 usado en
        la representación impresa del DTE y el XML crudo.
        """
        self.ensure_one()

        xml_envio_vals = {'move_id': move.id, 'company_id': move.company_id.id}

        if cod_dte in DTE_CODES_BOLETA:
            barcode_data = response['barcodes'][0]
            tipo_dte = barcode_data['TpoDTE']
            folio = barcode_data['Folio']
            xml_content = response['sii_xml_request']
            xml_envio_vals.update({
                'sii_send_ident': response.get('sii_send_ident'),
                'sii_xml_request': xml_content,
                'sii_xml_response': xml_content,
                # La API de boletas ya devuelve la imagen del timbre
                # renderizada: solo se redimensiona para la impresión.
                'sii_barcode_img': self._l10n_cl_resize_barcode_image(barcode_data['sii_barcode_img']),
            })
        else:
            doc = response[0] if isinstance(response, list) else response
            tipo_dte = doc['TipoDTE']
            folio = doc['Folio']
            xml_content = doc['sii_xml_request']
            xml_envio_vals.update({
                'sii_xml_request': xml_content,
                # Acá solo llega el texto del timbre (TED); xml.envio.create()
                # lo convierte a imagen PDF417 automáticamente (ver
                # xml_envio.py: get_barcode_img/pdf417bc).
                'sii_barcode': doc.get('sii_barcode'),
            })

        xml_envio_vals['name'] = f"T{tipo_dte}F{folio}"

        if move.xml_envio_id:
            move.xml_envio_id.unlink()
        xml_envio = self.env['xml.envio'].create(xml_envio_vals)
        move.write({'xml_envio_id': xml_envio.id})

        return self.env['ir.attachment'].create({
            'name': f"DTE_T{tipo_dte}F{folio}.xml",
            'res_id': move.id,
            'res_model': 'account.move',
            'type': 'binary',
            'datas': base64.b64encode((xml_content or '').encode()),
            'mimetype': 'application/xml',
            'description': _('DTE generado para el SII'),
        })

    def _l10n_cl_resize_barcode_image(self, image_b64):
        """Redimensiona la imagen del timbre (TED) que devuelve el SII
        para boletas al tamaño usado en la representación impresa.
        Migrado desde barcode_imagen() en account.move.
        """
        raw = base64.b64decode(image_b64)
        image = Image.open(BytesIO(raw))
        image = image.resize((320, 192))
        buffer = BytesIO()
        image.save(buffer, format='PNG')
        return base64.b64encode(buffer.getvalue()).decode()

    def _l10n_cl_process_send_response(self, move, response, cod_dte):
        """Guarda track_id / estado inicial tras el envío. Sustituye a
        post_proceso() de account.move.
        """
        self.ensure_one()
        vals = {}
        if cod_dte in DTE_CODES_BOLETA:
            # La boleta ya fue enviada en el mismo paso: hay Track ID real.
            vals['track_id'] = response.get('sii_send_ident')
            vals['estado_dte'] = response.get('status') or 'EnProceso'
        else:
            # La factura solo fue timbrada; el envío del sobre (y su
            # Track ID) llega en un paso posterior, fuera de este método.
            vals['estado_dte'] = 'EnProceso'
        move.write(vals)
