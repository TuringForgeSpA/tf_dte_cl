# -*- coding: utf-8 -*-
"""Integración con account_edi: el framework solo dispara el envío.

Ruta real: models/account_edi_format.py

La firma ocurre al publicar (account.move._post); aquí se transmite el sobre
guardado y se traduce el estado DTE al resultado que espera account_edi.
"""
import logging

from odoo import models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

DTE_CODE = 'tf_cl_dte'
SUCCESS_STATES = ('sent', 'accepted', 'accepted_objections', 'rejected')
RETRY_STATES = ('signed', 'send_unknown')


class AccountEdiFormat(models.Model):
    _inherit = 'account.edi.format'

    def _is_compatible_with_journal(self, journal):
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._is_compatible_with_journal(journal)
        return journal.type == 'sale' and bool(journal.tf_dte_cl_document_type)

    def _needs_web_services(self):
        return self.code == DTE_CODE or super()._needs_web_services()

    def _get_move_applicability(self, move):
        self.ensure_one()
        if self.code != DTE_CODE:
            return super()._get_move_applicability(move)
        if move.tf_dte_cl_is_dte:
            return {
                'post': self._tf_dte_cl_post_invoice_edi,
                'cancel': self._tf_dte_cl_cancel_invoice_edi,
            }
        return None

    def _check_move_configuration(self, move):
        errors = super()._check_move_configuration(move)
        if self.code != DTE_CODE or not move.tf_dte_cl_is_dte or move.tf_dte_cl_xml_file:
            return errors
        return errors + move._tf_dte_cl_readiness_errors()

    def _tf_dte_cl_post_invoice_edi(self, invoices):
        results = {}
        for move in invoices:
            try:
                with self.env.cr.savepoint():
                    state = move._tf_dte_cl_send()
            except UserError as error:
                self.env.invalidate_all()
                message = error.args[0] if error.args else str(error)
                move.tf_dte_cl_error = message
                results[move] = {'error': message, 'blocking_level': 'error'}
                continue
            except Exception as error:  # noqa: BLE001 - error de red o de la librería: se reintenta
                self.env.invalidate_all()
                _logger.exception('Error al enviar el DTE de %s', move.display_name)
                results[move] = {'error': str(error), 'blocking_level': 'warning'}
                continue
            if state in SUCCESS_STATES:
                # 'rejected' también es un envío completado: el rechazo se gestiona en el DTE.
                results[move] = {'success': True}
            elif state in RETRY_STATES:
                results[move] = {
                    'error': move.tf_dte_cl_error or self.env._('Envío pendiente; se reintentará.'),
                    'blocking_level': 'warning',
                }
            else:
                results[move] = {
                    'error': move.tf_dte_cl_error or self.env._('El DTE requiere revisión.'),
                    'blocking_level': 'error',
                }
        return results

    def _tf_dte_cl_cancel_invoice_edi(self, invoices):
        results = {}
        for move in invoices:
            try:
                with self.env.cr.savepoint():
                    move._tf_dte_cl_cancel_dte()
                results[move] = {'success': True}
            except UserError as error:
                self.env.invalidate_all()
                results[move] = {'error': error.args[0] if error.args else str(error), 'blocking_level': 'error'}
        return results
