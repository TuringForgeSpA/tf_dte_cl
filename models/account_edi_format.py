# -*- coding: utf-8 -*-
import logging

from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

try:
    from facturacion_electronica import facturacion_electronica as fe
except ImportError:
    fe = None
    _logger.warning(
        'No se pudo importar la librería "facturacion_electronica". '
        'Instálela con: pip install facturacion_electronica'
    )

DTE_CODE = 'tf_cl_dte'
ESTADOS_TERMINALES = ('Aceptado', 'Rechazado', 'Reparo')


class AccountEdiFormat(models.Model):
    _inherit = 'account.edi.format'

    # ------------------------------------------------------------------
    # Puntos de extensión del framework EDI
    # ------------------------------------------------------------------
    def _is_compatible_with_journal(self, journal):
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._is_compatible_with_journal(journal)
        return journal.type == 'sale' and bool(journal.cod_dte)

    def _needs_web_services(self):
        return self.code == DTE_CODE or super()._needs_web_services()

    def _is_required_for_invoice(self, move):
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._is_required_for_invoice(move)
        return bool(move.journal_id.cod_dte)

    def _get_move_applicability(self, move):
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._get_move_applicability(move)
        if move.move_type in ('out_invoice', 'out_refund') and move.journal_id.cod_dte:
            return {
                'post': self._tf_dte_cl_post_invoice_edi,
                'cancel': self._tf_dte_cl_cancel_invoice_edi,
                'edi_content': self._tf_dte_cl_get_content,
            }
        return None

    def _check_move_configuration(self, move):
        self.ensure_one()
        errores = super()._check_move_configuration(move)
        if self.code != DTE_CODE:
            return errores
        journal = move.journal_id
        conf = journal.config_dte_id
        if not journal.cod_dte:
            errores.append(_('El diario "%s" no tiene configurado un código de documento SII.') % journal.name)
            return errores
        if not conf:
            errores.append(_('El diario "%s" no tiene una configuración DTE asociada.') % journal.name)
            return errores
        if not any(caf.name == journal.cod_dte for caf in conf.caf_files_ids):
            errores.append(_(
                'No hay un CAF cargado en "%s" para el tipo de documento %s.'
            ) % (conf.name, journal.cod_dte))
        faltantes = move._dte_revisar_cliente(move.partner_id)
        if faltantes:
            errores.append(_(
                'Faltan datos del cliente "%s": %s.'
            ) % (move.partner_id.name, ', '.join(faltantes)))
        if journal.cod_dte == '61' and not move.referencias_ids:
            errores.append(_('Las notas de crédito electrónicas requieren al menos una referencia.'))
        return errores

    # ------------------------------------------------------------------
    # Envío: timbrado + transmisión al SII en un solo paso atómico
    # ------------------------------------------------------------------
    def _tf_dte_cl_post_invoice_edi(self, invoices):
        self.ensure_one()
        if fe is None:
            error = _('La librería "facturacion_electronica" no está instalada en el servidor.')
            return {move: {'error': error, 'blocking_level': 'error'} for move in invoices}

        resultados = {}
        invoices._dte_asignar_folio()
        for move in invoices:
            try:
                cod_dte = move.journal_id.cod_dte
                conf = move.journal_id.config_dte_id
                data = move._dte_build_envio(conf, cod_dte)
                if conf.modo == 'pruebas':
                    _logger.info('DTE en modo de pruebas, no se transmite al SII: %s', move.name)
                    resultados[move] = {'success': True}
                    continue
                respuesta = fe.timbrar_y_enviar(data)
                move._dte_procesar_respuesta_envio(respuesta, cod_dte)
                resultados[move] = {'success': True}
            except UserError as error:
                # Errores de datos/configuración: bloqueantes, requieren intervención del usuario.
                resultados[move] = {'error': str(error), 'blocking_level': 'error'}
            except Exception as error:  # noqa: BLE001 - errores de red/timeout de la librería externa
                _logger.exception('Error al timbrar/enviar el DTE de %s', move.name)
                resultados[move] = {'error': str(error), 'blocking_level': 'warning'}
        return resultados

    def _tf_dte_cl_cancel_invoice_edi(self, invoices):
        self.ensure_one()
        resultados = {}
        for move in invoices:
            if move.estado_dte in ESTADOS_TERMINALES and move.estado_dte != 'Rechazado':
                resultados[move] = {
                    'error': _(
                        'Un DTE aceptado por el SII no puede anularse por esta vía; '
                        'debe emitir una Nota de Crédito de referencia.'
                    ),
                    'blocking_level': 'error',
                }
            else:
                resultados[move] = {'success': True}
        return resultados

    def _tf_dte_cl_get_content(self, move):
        self.ensure_one()
        contenido = move.xml_envio_id.sii_xml_dte or move.xml_envio_id.sii_xml_request or ''
        return contenido.encode()

    # ------------------------------------------------------------------
    # Consulta de estado (ejecutada por cron, ver data/tf_dte_cl_cron.xml)
    # ------------------------------------------------------------------
    def _tf_dte_cl_consultar_estado(self, invoices):
        self.ensure_one()
        if fe is None:
            _logger.warning('No se puede consultar el estado DTE: falta la librería "facturacion_electronica".')
            return
        for move in invoices:
            try:
                respuesta = fe.consulta_estado_dte(move._dte_build_consulta())
                move._dte_procesar_respuesta_consulta(respuesta)
            except Exception:  # noqa: BLE001 - no debe romper el cron para el resto del batch
                _logger.exception('Error al consultar el estado SII del DTE %s', move.name)
