# -*- coding: utf-8 -*-
"""Tipo de DTE y sucursal de los diarios de venta.

Ruta real: models/account_journal.py
"""
from odoo import api, fields, models
from odoo.exceptions import ValidationError

JOURNAL_DTE_TYPES = [
    ('33', 'Factura electrónica (33)'),
    ('34', 'Factura no afecta o exenta electrónica (34)'),
    ('56', 'Nota de débito electrónica (56)'),
    ('61', 'Nota de crédito electrónica (61)'),
]
EDI_FORMAT_XMLID = 'tf_dte_cl.edi_format_tf_cl_dte'


class AccountJournal(models.Model):
    _inherit = 'account.journal'

    tf_dte_cl_document_type = fields.Selection(
        JOURNAL_DTE_TYPES, string='Tipo DTE',
        help='Documento tributario electrónico que emite este diario. Use un diario por tipo: '
             'las notas de crédito requieren su propio diario (61).',
    )
    tf_dte_cl_branch_id = fields.Many2one(
        'tf_dte_cl.branch', string='Sucursal SII', check_company=True,
        domain="[('company_id', '=', company_id)]",
    )

    @api.constrains('tf_dte_cl_document_type', 'type')
    def _check_tf_dte_cl_document_type(self):
        for journal in self:
            if journal.tf_dte_cl_document_type and journal.type != 'sale':
                raise ValidationError(self.env._('Solo los diarios de venta pueden emitir DTE.'))

    def _tf_dte_cl_sync_edi_format(self):
        edi_format = self.env.ref(EDI_FORMAT_XMLID, raise_if_not_found=False)
        if not edi_format:
            return
        for journal in self:
            if journal.tf_dte_cl_document_type and edi_format not in journal.edi_format_ids:
                journal.edi_format_ids = [fields.Command.link(edi_format.id)]
            elif not journal.tf_dte_cl_document_type and edi_format in journal.edi_format_ids:
                journal.edi_format_ids = [fields.Command.unlink(edi_format.id)]

    @api.model_create_multi
    def create(self, vals_list):
        journals = super().create(vals_list)
        journals.filtered('tf_dte_cl_document_type')._tf_dte_cl_sync_edi_format()
        return journals

    def write(self, vals):
        res = super().write(vals)
        if 'tf_dte_cl_document_type' in vals:
            self._tf_dte_cl_sync_edi_format()
        return res
