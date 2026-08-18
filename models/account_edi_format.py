# -*- coding: utf-8 -*-
import ast
import base64
import collections
import logging
from io import BytesIO

from lxml import etree
from PIL import Image

from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ==========================================================
# IMPORTACIÓN DEFENSIVA DE LAS DEPENDENCIAS EXTERNAS
# ==========================================================
# La librería ya NO se vendoriza dentro del módulo (antes:
# "from .facturacion_electronica import facturacion_electronica as fe").
# Ahora se instala vía pip y se declara en __manifest__.py bajo
# external_dependencies. Se importa de forma defensiva para que la
# ausencia de la librería NO impida cargar el módulo: el error se
# reporta al usuario en _check_move_configuration() y en el banner
# EDI de la factura, en vez de reventar el arranque de Odoo.
try:
    from facturacion_electronica import facturacion_electronica as fe
except ImportError:  # pragma: no cover
    fe = None
    _logger.warning(
        "No se pudo importar la librería 'facturacion_electronica'. "
        "El formato EDI del SII quedará deshabilitado hasta que se instale "
        "(pip install facturacion_electronica)."
    )

try:
    import requests
except ImportError:  # pragma: no cover - requests viene con Odoo, esto es solo defensivo
    requests = None

# Código técnico único de este formato. Debe coincidir EXACTO con el
# <field name="code">sii_dte</field> del registro en data/account_edi_data.xml
DTE_CODE = 'sii_dte'

# cod_dte (código de documento SII) que hoy vive en account.journal.
DTE_CODES_FACTURACION = ['33', '34', '43', '46', '52', '56', '61']  # Facturas, NC, ND, Guías, etc.
DTE_CODES_BOLETA = ['39', '41']  # Boletas: se migran a tf_dte_cl_boleta; la rama se conserva por compatibilidad.
DTE_CODES_SOPORTADOS = DTE_CODES_FACTURACION + DTE_CODES_BOLETA

# RUT al que se dirige el SOBRE (SetDTE), no el receptor comercial del
# documento. 60803000-K es el RUT del propio SII. Valor heredado tal cual
# del módulo antiguo (enviar_dte/crear_dte).
RUT_RECEPTOR_ENVIO = '60803000-K'

# ==========================================================
# ESTADOS NORMALIZADOS
# ==========================================================
# Únicos cuatro valores que _l10n_cl_query_dte_status puede devolver y
# que _l10n_cl_update_dte_status sabe traducir al vocabulario del
# framework EDI. Cualquier otro string del SII se normaliza a uno de
# estos o, si no se reconoce, a EnProceso (conservador: se vuelve a
# consultar en el siguiente cron en vez de dar por cerrado el documento).
ESTADO_ACEPTADO = 'Aceptado'
ESTADO_RECHAZADO = 'Rechazado'
ESTADO_REPARO = 'Reparo'
ESTADO_EN_PROCESO = 'EnProceso'

# Códigos que devuelve el webservice de consulta de estado de envío del
# SII (campo ESTADO de la respuesta de QueryEstUp) y sus equivalentes en
# texto que a veces entrega la librería en response['status'].
# OJO: cuando el sobre ya fue procesado, mandan los CONTADORES
# (ACEPTADOS/RECHAZADOS/REPAROS), no este código; ver
# _l10n_cl_classify_estado. Revisar esta tabla contra la documentación
# vigente del SII si aparecen estados sin reconocer en el log.
SII_ESTADO_ACEPTADO = {'EPR', 'DOK', 'MMC', 'TMC', 'AND', 'ANC', 'ACEPTADO'}
SII_ESTADO_REPARO = {'DNK', 'RPR', 'REPARO', 'CONREPARO', 'ACEPTADOCONREPARO', 'ACEPTADOCONREPAROS'}
SII_ESTADO_RECHAZADO = {
    'RCT', 'RCH', 'RFR', 'RSC', 'RPT', 'RFP', 'SNC', 'FNA', 'FAU', 'FAN',
    'LRH', 'RCR', 'RDC', 'VOF', 'RECHAZADO',
}
SII_ESTADO_EN_PROCESO = {
    'SOK', 'CRT', 'PDR', 'FOK', 'LOK', 'PRD', '0', 'ENPROCESO', 'PROCESO',
    'ENVIADO', 'RECIBIDO',
}

# Traducción del estado normalizado al Selection de xml.envio (registro
# de auditoría). NOTA: xml.envio.ESTADOS no contempla "Reparo"; hasta que
# se agregue esa opción en xml_envio.py, un DTE aceptado con reparos se
# refleja como Aceptado en la auditoría (legalmente lo está) y el detalle
# queda en sii_receipt / move.detalle_estado.
XML_ENVIO_STATE_MAP = {
    ESTADO_ACEPTADO: 'Aceptado',
    ESTADO_RECHAZADO: 'Rechazado',
    ESTADO_REPARO: 'Aceptado',
    ESTADO_EN_PROCESO: 'EnProceso',
}


class SiiEdiError(Exception):
    """Excepción interna: transporta un error del SII ya clasificado
    (mensaje legible + severidad) desde las llamadas a la librería `fe`
    hasta `_post_invoice_edi`, listo para convertirse en el dict que el
    framework EDI de Odoo espera para pintar el banner de la factura.
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

      - HTTP 4xx (schema, firma, RUT receptor...): reenviar el MISMO XML
        volverá a fallar -> 'error'.
      - HTTP 5xx / timeout / caída de conexión: transitorio -> 'warning'
        (el cron de reintentos de account.edi.document lo resuelve solo).
      - Cualquier otra excepción de la librería (CAF agotado, firma
        inválida...): bloqueante por defecto.
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
    """
    _inherit = 'account.edi.format'

    # ==========================================================
    # 1. APLICABILIDAD Y COMPATIBILIDAD
    # ==========================================================

    def _get_move_applicability(self, move):
        """Punto de entrada central del framework: por cada
        account.edi.format instalado, Odoo pregunta "¿te haces cargo de
        este documento?". Sustituye al chequeo manual de cod_dte que hoy
        vive en account.move._post().
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
        """Regla de negocio pura: ¿este move debería generar un DTE?"""
        self.ensure_one()
        if move.move_type not in ('out_invoice', 'out_refund'):
            return False
        if move.country_code != 'CL':
            return False
        cod_dte = move.journal_id.cod_dte
        return bool(cod_dte) and cod_dte in DTE_CODES_SOPORTADOS

    def _is_compatible_with_journal(self, journal):
        """Controla si este formato aparece seleccionable en la
        configuración "Electronic Invoicing" del diario.
        """
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._is_compatible_with_journal(journal)
        return journal.type == 'sale' and journal.country_code == 'CL' and bool(journal.cod_dte)

    def _needs_web_services(self):
        """True: timbrar y transmitir contra los webservices del SII. Esto
        hace que el framework use la cola asíncrona de
        account.edi.document (to_send -> sent) en vez de adjuntar el XML
        de forma síncrona al confirmar.
        """
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._needs_web_services()
        return True

    def _is_required_for_invoice(self, invoice):
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._is_required_for_invoice(invoice)
        return self._l10n_cl_is_dte_applicable(invoice)

    def _support_batching(self, move=None, state=None, company=None):
        """Se envía documento a documento: cada factura genera su propio
        sobre y su propio Track ID. El batching real (RCOF / consumo de
        folios) es harina de otro costal.
        """
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._support_batching(move=move, state=state, company=company)
        return False

    # ==========================================================
    # 2. VALIDACIÓN PREVIA AL ENVÍO
    # ==========================================================

    def _check_move_configuration(self, move):
        """Se ejecuta ANTES de intentar enviar. Devuelve una lista de
        strings; si no está vacía, Odoo bloquea el envío sin llegar a
        llamar a _post_invoice_edi.
        """
        self.ensure_one()
        errors = super()._check_move_configuration(move)
        if self.code != DTE_CODE:
            return errors

        # Dependencia externa: se avisa acá (bloqueo limpio) en vez de
        # dejar reventar la llamada dentro de _post_invoice_edi.
        if fe is None:
            errors.append(_(
                "No está instalada la librería Python 'facturacion_electronica'. "
                "Instálela en el entorno de Odoo (pip install facturacion_electronica) "
                "y reinicie el servicio."
            ))

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

        Devuelve {move: {...}}:
          - éxito -> {'success': True, 'attachment': <ir.attachment>}
          - error -> {'error': '<mensaje>', 'blocking_level': 'error'|'warning'}
        """
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._post_invoice_edi(invoices)

        result = {}
        for move in invoices:
            cod_dte = move.journal_id.cod_dte
            conf = move.journal_id.config_dte_id
            try:
                # Todo lo que toca la BD para ESTE move queda dentro del
                # savepoint: si algo falla a mitad de camino solo se
                # deshace lo de este move, no el lote completo.
                with self.env.cr.savepoint():
                    response = self._l10n_cl_send_dte(move, conf, cod_dte)
                    attachment = self._l10n_cl_create_dte_attachment(move, response, cod_dte)
                    self._l10n_cl_process_send_response(move, response, cod_dte)
                result[move] = {
                    'success': True,
                    'attachment': attachment,
                }
            except SiiEdiError as error:
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
        """En Chile un DTE ya ACEPTADO no se anula por este mecanismo: se
        anula emitiendo una Nota de Crédito referenciada (cod_dte 61).
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
        """Llamado por un ir.cron periódico (data/ir_cron_data.xml).
        Sustituye la llamada manual a consulta_estado_dte().
        """
        edi_format = self.search([('code', '=', DTE_CODE)], limit=1)
        if not edi_format:
            return

        documents = self.env['account.edi.document'].search([
            ('edi_format_id', '=', edi_format.id),
            ('state', '=', 'sent'),
            ('move_id.track_id', '!=', False),
            # Aceptado / Rechazado / Reparo son respuestas DEFINITIVAS del
            # SII: si no se excluye Reparo, esos documentos se seguirían
            # consultando indefinidamente en cada pasada del cron.
            ('move_id.estado_dte', 'not in', [ESTADO_ACEPTADO, ESTADO_RECHAZADO, ESTADO_REPARO]),
        ])
        for document in documents:
            edi_format._l10n_cl_update_dte_status(document)

    def _l10n_cl_update_dte_status(self, document):
        """Consulta el estado de UN account.edi.document contra el SII
        (usando move.track_id) y lo traduce al vocabulario del framework.

        Nunca propaga excepciones: se ejecuta desde un cron sobre un lote
        de documentos y un SII caído no debe abortar la pasada completa.
        """
        self.ensure_one()
        move = document.move_id
        cod_dte = move.journal_id.cod_dte

        try:
            estado, glosa = self._l10n_cl_query_dte_status(move, cod_dte)
        except SiiEdiError as error:
            if error.blocking_level == 'error':
                # Problema que no se resuelve solo (firma, Track ID
                # inválido, configuración): se refleja en el banner.
                document.write({'error': error.message, 'blocking_level': 'error'})
            else:
                # Transitorio (5xx, timeout, sin respuesta): se reintenta
                # en la próxima pasada sin ensuciar la factura.
                _logger.info("No se pudo consultar el estado de %s: %s", move.name, error.message)
            return
        except Exception:
            _logger.exception("Error inesperado consultando el estado del DTE %s", move.name)
            return

        vals = {}
        if move.estado_dte != estado:
            vals['estado_dte'] = estado
        if move.detalle_estado != glosa:
            vals['detalle_estado'] = glosa
        if vals:
            move.write(vals)

        if move.xml_envio_id:
            xml_vals = {'state': XML_ENVIO_STATE_MAP.get(estado, 'EnProceso')}
            if glosa:
                xml_vals['sii_receipt'] = glosa
            move.xml_envio_id.write(xml_vals)

        if estado == ESTADO_RECHAZADO:
            document.write({
                'error': glosa or _("El SII rechazó el DTE."),
                'blocking_level': 'error',
            })
        elif estado == ESTADO_REPARO:
            document.write({
                'error': glosa or _("El SII aceptó el DTE con reparos."),
                'blocking_level': 'warning',
            })
        elif estado == ESTADO_ACEPTADO:
            document.write({'error': False, 'blocking_level': False})
        # si sigue "EnProceso" no se toca el account.edi.document: se reintenta después

    # ==========================================================
    # 5. CONTENIDO / NOMBRE DE ARCHIVO
    # ==========================================================

    def _get_invoice_edi_content(self, move):
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._get_invoice_edi_content(move)
        if move.xml_envio_id and move.xml_envio_id.sii_xml_request:
            return move.xml_envio_id.sii_xml_request.encode()
        return b""

    def _get_invoice_edi_filename(self, move):
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._get_invoice_edi_filename(move)
        cod_dte = move.journal_id.cod_dte or ''
        folio = move.folio(move.name)
        return f"DTE_T{cod_dte}F{folio}.xml"

    # ==========================================================
    # 6. HELPERS INTERNOS
    # ==========================================================

    def _l10n_cl_build_dte_payload(self, move, conf, cod_dte):
        """Arma el diccionario base que consume la librería `fe`.

        Idéntico al que armaban crear_dte() / enviar_dte() en el módulo
        antiguo; se extrae a su propio método porque ahora se usa DOS
        veces (timbrado y envío del sobre).
        """
        self.ensure_one()
        data = collections.OrderedDict()
        data['Emisor'] = move.data_emisor(conf)
        # RUT del SOBRE (SII), no del receptor comercial: se preserva el
        # valor fijo del código original.
        data['RutReceptor'] = RUT_RECEPTOR_ENVIO
        data['firma_electronica'] = move.data_firma_electronica(conf)
        data['Documento'] = move.data_documento(move, conf, cod_dte)
        data['api'] = move.api_dte(cod_dte)
        return data

    def _l10n_cl_call_fe(self, move, method_name, data):
        """Único punto de llamada a la librería facturacion_electronica.

        Centraliza el manejo de errores: cualquier excepción cruda se
        clasifica (HTTP 4xx -> 'error', 5xx/timeout -> 'warning') y se
        re-lanza como SiiEdiError, ya lista para el banner EDI.
        """
        self.ensure_one()
        if fe is None:
            raise SiiEdiError(
                _("La librería 'facturacion_electronica' no está instalada en el servidor. "
                  "No es posible timbrar ni enviar DTEs."),
                'error',
            )

        method = getattr(fe, method_name, None)
        if method is None:
            raise SiiEdiError(
                _("La versión instalada de 'facturacion_electronica' no expone %s(). "
                  "Revise la versión declarada en external_dependencies.") % method_name,
                'error',
            )

        try:
            response = method(data)
        except SiiEdiError:
            raise
        except Exception as error:
            message, blocking_level = _classify_sii_exception(error)
            _logger.warning("SII falló en %s() para %s: %s", method_name, move.name, error)
            raise SiiEdiError(message, blocking_level) from error

        if not response:
            raise SiiEdiError(
                _("El SII no devolvió respuesta al ejecutar %s para el documento %s.") % (method_name, move.name),
                'warning',
            )
        return response

    def _l10n_cl_send_dte(self, move, conf, cod_dte):
        """Timbra el DTE y lo envía efectivamente al SII, devolviendo una
        respuesta NORMALIZADA (misma forma para factura y boleta).

        ---------------------------------------------------------------
        CORRECCIÓN vs. la versión anterior
        ---------------------------------------------------------------
        La versión anterior llamaba solo a fe.timbrar() para las
        facturas: el DTE quedaba firmado y con su TED, pero el sobre
        (SetDTE) nunca se transmitía y por lo tanto NUNCA había Track ID.

        En el módulo antiguo esto eran dos pasos separados:
          1. crear_dte() -> timbrar_dte() -> fe.timbrar(data)
             genera el DTE timbrado y su sii_barcode (TED) para la
             representación impresa.
          2. enviar_dte() (botón manual) -> fe.timbrar_y_enviar(data)
             con data['ID'] = 'Env<id>', que es la llamada que realmente
             deposita el sobre en el SII y devuelve 'sii_send_ident'
             (el Track ID) — ver post_proceso() del módulo antiguo.

        Ambos pasos se ejecutan ahora dentro del mismo _post_invoice_edi,
        de forma asíncrona vía account.edi.document (to_send -> sent).

        Se mantienen las dos llamadas (y no solo timbrar_y_enviar) porque
        la respuesta del sobre NO trae el TED por documento: el
        sii_barcode que necesita el PDF417 de la impresión solo viene en
        la respuesta de fe.timbrar(). Timbrar dos veces no consume folio
        (el folio sale de move.name y el CAF ya está asignado).
        """
        self.ensure_one()

        data = self._l10n_cl_build_dte_payload(move, conf, cod_dte)

        # Modo 'pruebas': el módulo antiguo solo logueaba el payload y no
        # tocaba el SII. Se preserva ese comportamiento, pero se reporta
        # como warning para no marcar como "enviado" algo que nunca salió.
        if (data.get('Emisor') or {}).get('Modo') == 'pruebas':
            _logger.info("[DTE pruebas] Payload de %s (no se envía al SII): %s", move.name, data)
            raise SiiEdiError(
                _("La configuración DTE está en modo 'pruebas': el documento no se transmitió al SII."),
                'warning',
            )

        # ---------- Boletas (39/41): un solo paso ----------
        # Migran a tf_dte_cl_boleta; la rama se conserva por compatibilidad.
        if cod_dte in DTE_CODES_BOLETA:
            response = self._l10n_cl_call_fe(move, 'timbrar_y_enviar', data)
            return self._l10n_cl_normalize_boleta_response(move, response, cod_dte)

        # ---------- Facturas / NC / ND / Guías: DOS pasos ----------

        # PASO 1: timbrar. Devuelve una lista de documentos con
        # TipoDTE, Folio, sii_xml_request (DTE firmado) y sii_barcode (TED).
        stamped = self._l10n_cl_call_fe(move, 'timbrar', data)
        doc = stamped[0] if isinstance(stamped, (list, tuple)) else stamped
        if not isinstance(doc, dict) or not doc.get('sii_xml_request'):
            raise SiiEdiError(
                _("El timbrado no devolvió un DTE válido para %s. Revise el CAF y la firma electrónica.") % move.name,
                'error',
            )

        # PASO 2: envío efectivo del sobre (SetDTE) al SII.
        # 'ID' es el identificador del sobre y la URI de referencia de la
        # firma. El módulo antiguo usaba 'Env<id de xml.envio>'; ahora
        # xml.envio se crea DESPUÉS del envío (es solo registro de
        # auditoría), así que se ancla al id del move: estable, único y
        # sin dependencias de orden.
        envio_data = collections.OrderedDict(data)
        envio_data['ID'] = 'Env%s' % move.id

        envelope = self._l10n_cl_call_fe(move, 'timbrar_y_enviar', envio_data)
        if not isinstance(envelope, dict):
            raise SiiEdiError(
                _("Respuesta inesperada del SII al enviar el sobre de %s.") % move.name,
                'error',
            )

        # PASO 3: capturar el Track ID.
        track_id = envelope.get('sii_send_ident')
        if not track_id:
            # blocking_level='error' a propósito: si el sobre pudo haber
            # entrado sin que se devolviera el ident, un reintento
            # automático generaría un envío duplicado ("Archivo Repetido",
            # código 90). Que lo revise una persona.
            _logger.error("Envío de %s sin Track ID. Respuesta del SII: %s", move.name, envelope)
            raise SiiEdiError(
                _("El SII no devolvió un Track ID para %s. Verifique en el portal del SII si el envío "
                  "quedó registrado ANTES de reintentar, para no duplicar el sobre.") % move.name,
                'error',
            )

        _logger.info("DTE %s enviado al SII. Track ID: %s", move.name, track_id)

        return {
            'tipo_dte': doc.get('TipoDTE') or int(cod_dte),
            'folio': doc.get('Folio') or move.folio(move.name),
            'sii_xml_request': doc.get('sii_xml_request'),      # DTE timbrado
            'sii_xml_envio': envelope.get('sii_xml_request'),   # sobre SetDTE transmitido
            'sii_barcode': doc.get('sii_barcode'),              # TED en texto -> xml.envio genera el PDF417
            'sii_barcode_img': False,
            'track_id': track_id,
            'status': envelope.get('status') or 'EnProceso',
        }

    def _l10n_cl_normalize_boleta_response(self, move, response, cod_dte):
        """Normaliza la respuesta de fe.timbrar_y_enviar() para boletas a
        la misma estructura que devuelve la rama de facturas.
        """
        self.ensure_one()
        barcodes = response.get('barcodes') or []
        barcode = barcodes[0] if barcodes else {}
        barcode_img = barcode.get('sii_barcode_img')
        xml_content = response.get('sii_xml_request')

        return {
            'tipo_dte': barcode.get('TpoDTE') or int(cod_dte),
            'folio': barcode.get('Folio') or move.folio(move.name),
            'sii_xml_request': xml_content,
            'sii_xml_envio': xml_content,
            'sii_barcode': False,
            # La API de boletas ya devuelve la imagen renderizada: solo se
            # redimensiona para la impresión.
            'sii_barcode_img': self._l10n_cl_resize_barcode_image(barcode_img) if barcode_img else False,
            'track_id': response.get('sii_send_ident'),
            'status': response.get('status') or 'EnProceso',
        }

    def _l10n_cl_query_dte_status(self, move, cod_dte):
        """Consulta el Track ID contra el SII y devuelve una tupla
        (estado, glosa) NORMALIZADA, donde estado es estrictamente uno de
        'Aceptado' / 'Rechazado' / 'Reparo' / 'EnProceso'.

        Migrado de consulta_estado_dte() de account.move, que mezclaba la
        consulta con la escritura de campos; acá la consulta es pura
        (salvo la copia del XML crudo al registro de auditoría) y quien
        decide qué hacer con el resultado es _l10n_cl_update_dte_status.

        Igual que en el módulo antiguo, el webservice depende del tipo:
          - Boletas (39/41): fe.consulta_estado_documento(), que además
            necesita el 'Documento' completo en el payload.
          - Resto: fe.consulta_estado_dte().

        Los errores de red/HTTP los clasifica _l10n_cl_call_fe y viajan
        como SiiEdiError (5xx/timeout -> 'warning' y se reintenta;
        4xx/firma -> 'error' y se muestra en el banner).
        """
        self.ensure_one()

        conf = move.journal_id.config_dte_id
        if not conf:
            raise SiiEdiError(
                _("El diario '%s' no tiene configuración DTE: no es posible consultar el estado.")
                % move.journal_id.name,
                'error',
            )
        if not move.track_id:
            # Sin Track ID no hay nada que consultar: el documento sigue
            # pendiente de envío, no es un error del SII.
            return ESTADO_EN_PROCESO, _("El documento todavía no tiene Track ID asignado.")

        data = collections.OrderedDict()
        data['Emisor'] = move.data_emisor(conf)
        data['firma_electronica'] = move.data_firma_electronica(conf)
        data['codigo_envio'] = move.track_id

        if cod_dte in DTE_CODES_BOLETA:
            # consulta_estado_documento necesita el detalle del documento
            # para reconstruir la clave de consulta (tal como lo hacía
            # consulta_estado_dte() en el módulo antiguo).
            data['Documento'] = move.data_documento(move, conf, cod_dte)
            response = self._l10n_cl_call_fe(move, 'consulta_estado_documento', data)
            estado, glosa, raw_response = self._l10n_cl_parse_boleta_status(move, response)
        else:
            response = self._l10n_cl_call_fe(move, 'consulta_estado_dte', data)
            estado, glosa, raw_response = self._l10n_cl_parse_dte_status(move, response)

        # Se conserva la respuesta cruda en el registro de auditoría, como
        # hacía el write de sii_xml_response del módulo antiguo.
        if raw_response and move.xml_envio_id:
            move.xml_envio_id.write({'sii_xml_response': raw_response})

        _logger.info("Estado SII de %s (Track ID %s): %s", move.name, move.track_id, estado)
        return estado, glosa

    def _l10n_cl_parse_dte_status(self, move, response):
        """Parsea la respuesta de fe.consulta_estado_dte().

        Migrado de procesar_respuesta_xml(): lee ESTADO, GLOSA y los
        contadores ACEPTADOS / RECHAZADOS / REPAROS del XML de respuesta.

        Devuelve (estado_normalizado, glosa, xml_crudo).
        """
        self.ensure_one()
        if not isinstance(response, dict):
            return ESTADO_EN_PROCESO, _("Respuesta inesperada del SII al consultar el estado."), ''

        raw_response = response.get('xml_resp') or ''
        estado_sii = (response.get('status') or '').strip()
        glosa_sii = ''
        contadores = {'ACEPTADOS': 0, 'RECHAZADOS': 0, 'REPAROS': 0}

        if raw_response:
            try:
                xml_bytes = raw_response.encode('utf-8') if isinstance(raw_response, str) else raw_response
                root = etree.fromstring(xml_bytes)
                for element in root.iter():
                    if not isinstance(element.tag, str):  # comentarios / PIs
                        continue
                    tag = etree.QName(element).localname  # tolera namespaces
                    text = (element.text or '').strip()
                    if not text:
                        continue
                    if tag == 'ESTADO':
                        estado_sii = text
                    elif tag == 'GLOSA':
                        glosa_sii = text
                    elif tag in contadores:
                        try:
                            contadores[tag] = int(text)
                        except ValueError:
                            contadores[tag] = 0
            except etree.XMLSyntaxError:
                _logger.warning(
                    "No se pudo parsear la respuesta de estado del SII para %s: %s",
                    move.name, raw_response[:500],
                )

        estado = self._l10n_cl_classify_estado(estado_sii, contadores)
        glosa = self._l10n_cl_build_glosa(estado_sii, glosa_sii, contadores)
        return estado, glosa, raw_response

    def _l10n_cl_parse_boleta_status(self, move, response):
        """Parsea la respuesta de fe.consulta_estado_documento().

        Migrado de procesar_respuesta_boleta(): la librería devuelve un
        dict indexado por el nombre del envío ('T<tipo>F<folio>', el mismo
        que se guarda en xml.envio.name), y dentro trae 'status', 'glosa'
        y un 'xml_resp' que es un dict serializado como texto con
        'codigo' y 'descripcion'.

        Devuelve (estado_normalizado, glosa, respuesta_cruda).
        """
        self.ensure_one()
        if not isinstance(response, dict):
            return ESTADO_EN_PROCESO, _("Respuesta inesperada del SII al consultar la boleta."), ''

        clave = move.xml_envio_id.name if move.xml_envio_id else False
        detalle = response.get(clave) if clave else None
        if not isinstance(detalle, dict):
            # Si la clave no calza (envío regenerado, nombre distinto),
            # se acepta la respuesta cuando trae un único documento.
            candidatos = [v for v in response.values() if isinstance(v, dict)]
            detalle = candidatos[0] if len(candidatos) == 1 else None
        if not isinstance(detalle, dict):
            _logger.info("El SII no devolvió detalle para la boleta %s (clave '%s').", move.name, clave)
            return ESTADO_EN_PROCESO, _("El SII todavía no informa el estado de este documento."), ''

        estado_sii = (detalle.get('status') or '').strip()
        glosa_sii = (detalle.get('glosa') or '').strip()
        raw_response = detalle.get('xml_resp') or ''

        codigo = ''
        descripcion = ''
        if raw_response:
            try:
                parsed = ast.literal_eval(raw_response) if isinstance(raw_response, str) else raw_response
                if isinstance(parsed, dict):
                    codigo = str(parsed.get('codigo') or '').strip()
                    descripcion = str(parsed.get('descripcion') or '').strip()
            except (ValueError, SyntaxError):
                _logger.warning(
                    "No se pudo interpretar xml_resp de la boleta %s: %s", move.name, str(raw_response)[:500]
                )

        estado = self._l10n_cl_classify_estado(estado_sii, {})
        # Si el status no es concluyente pero el SII ya entregó un código
        # de recepción distinto de 0 (1 schema, 2 firma, 3 RUT receptor,
        # 90 archivo repetido...), el documento está rechazado.
        if estado == ESTADO_EN_PROCESO and not estado_sii and codigo and codigo != '0':
            estado = ESTADO_RECHAZADO

        partes = [p for p in (estado_sii, glosa_sii, codigo and ' - '.join(filter(None, (codigo, descripcion)))) if p]
        glosa = ' - '.join(partes) or _("Sin detalle informado por el SII.")
        return estado, glosa, str(raw_response)

    def _l10n_cl_classify_estado(self, estado_sii, contadores):
        """Normaliza el estado crudo del SII a uno de los cuatro valores
        que entiende el framework EDI.

        Prioridad: los CONTADORES mandan sobre el código de estado. Una
        vez que el sobre fue procesado (ESTADO=EPR), quien dice si el DTE
        quedó aceptado, con reparos o rechazado es el desglose
        ACEPTADOS/RECHAZADOS/REPAROS, no el estado del envío.

        Ante un código desconocido devuelve EnProceso a propósito: es
        preferible volver a consultar en la siguiente pasada del cron
        antes que marcar como aceptado o rechazado algo que no se
        entendió.
        """
        self.ensure_one()
        contadores = contadores or {}
        if contadores.get('RECHAZADOS'):
            return ESTADO_RECHAZADO
        if contadores.get('REPAROS'):
            return ESTADO_REPARO
        if contadores.get('ACEPTADOS'):
            return ESTADO_ACEPTADO

        codigo = (estado_sii or '').strip().upper().replace(' ', '').replace('_', '')
        if not codigo:
            return ESTADO_EN_PROCESO
        if codigo in SII_ESTADO_RECHAZADO:
            return ESTADO_RECHAZADO
        if codigo in SII_ESTADO_REPARO:
            return ESTADO_REPARO
        if codigo in SII_ESTADO_ACEPTADO:
            return ESTADO_ACEPTADO
        if codigo in SII_ESTADO_EN_PROCESO:
            return ESTADO_EN_PROCESO

        _logger.warning(
            "Estado del SII no reconocido: '%s'. Se mantiene EnProceso; "
            "considere agregarlo a las tablas SII_ESTADO_*.", estado_sii,
        )
        return ESTADO_EN_PROCESO

    def _l10n_cl_build_glosa(self, estado_sii, glosa_sii, contadores):
        """Arma el texto legible que va a move.detalle_estado y al banner.
        Equivale al string que devolvía procesar_respuesta_xml().
        """
        self.ensure_one()
        partes = [p for p in (estado_sii, glosa_sii) if p]
        if contadores and any(contadores.values()):
            partes.append(_("Aceptados: %(ok)s, Rechazados: %(ko)s, Reparos: %(rep)s") % {
                'ok': contadores.get('ACEPTADOS', 0),
                'ko': contadores.get('RECHAZADOS', 0),
                'rep': contadores.get('REPAROS', 0),
            })
        return ' - '.join(partes) or _("Sin detalle informado por el SII.")

    def _l10n_cl_create_dte_attachment(self, move, response, cod_dte):
        """Crea el ir.attachment estándar (lo que el framework espera en
        'attachment') y, en paralelo, el registro `xml.envio`, que ya NO
        es la fuente de verdad del estado EDI (eso lo hace
        account.edi.document) pero se conserva para auditoría e
        impresión (código de barras PDF417 + XML crudo).

        Trabaja sobre la respuesta NORMALIZADA de _l10n_cl_send_dte.
        """
        self.ensure_one()

        tipo_dte = response['tipo_dte']
        folio = response['folio']
        track_id = response.get('track_id')

        xml_envio_vals = {
            'name': f"T{tipo_dte}F{folio}",
            'move_id': move.id,
            'company_id': move.company_id.id,
            'sii_xml_request': response.get('sii_xml_request'),
            'sii_xml_dte': response.get('sii_xml_request'),
            'sii_xml_response': response.get('sii_xml_envio'),
            'sii_send_ident': track_id,
            # El estado detallado del SII lo escribe el cron de Track ID;
            # acá solo se refleja que el sobre ya salió.
            'state': 'EnProceso' if track_id else 'NoEnviado',
        }
        if response.get('sii_barcode'):
            # Solo el texto del TED: xml.envio.create() lo convierte a
            # imagen PDF417 automáticamente (get_barcode_img/pdf417bc).
            xml_envio_vals['sii_barcode'] = response['sii_barcode']
        if response.get('sii_barcode_img'):
            xml_envio_vals['sii_barcode_img'] = response['sii_barcode_img']

        if move.xml_envio_id:
            # xml.envio.unlink() bloquea si el registro previo está en
            # Aceptado/Enviado/EnProceso: eso es deseable, evita pisar un
            # sobre que ya está en el SII.
            move.xml_envio_id.unlink()
        xml_envio = self.env['xml.envio'].create(xml_envio_vals)
        move.write({'xml_envio_id': xml_envio.id})

        # El adjunto EDI es lo efectivamente transmitido al SII (el
        # sobre); si no hubiera sobre, se cae al DTE timbrado.
        xml_content = response.get('sii_xml_envio') or response.get('sii_xml_request') or ''

        return self.env['ir.attachment'].create({
            'name': f"DTE_T{tipo_dte}F{folio}.xml",
            'res_id': move.id,
            'res_model': 'account.move',
            'type': 'binary',
            'datas': base64.b64encode(xml_content.encode()),
            'mimetype': 'application/xml',
            'description': _('DTE enviado al SII'),
        })

    def _l10n_cl_resize_barcode_image(self, image_b64):
        """Redimensiona la imagen del timbre (TED) que devuelve el SII
        para boletas al tamaño usado en la impresión. Migrado desde
        barcode_imagen() en account.move.
        """
        raw = base64.b64decode(image_b64)
        image = Image.open(BytesIO(raw))
        image = image.resize((320, 192))
        buffer = BytesIO()
        image.save(buffer, format='PNG')
        return base64.b64encode(buffer.getvalue()).decode()

    def _l10n_cl_process_send_response(self, move, response, cod_dte):
        """Guarda Track ID y estado inicial tras el envío. Sustituye a
        post_proceso() de account.move.

        Ahora es idéntico para factura y boleta: ambas ramas de
        _l10n_cl_send_dte devuelven un Track ID real, porque la factura
        ya sale efectivamente enviada.
        """
        self.ensure_one()
        move.write({
            'track_id': response.get('track_id'),
            'estado_dte': response.get('status') or 'EnProceso',
        })
