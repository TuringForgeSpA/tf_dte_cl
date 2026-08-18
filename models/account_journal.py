# -*- coding: utf-8 -*-
from odoo import api, fields, models


class AccountJournal(models.Model):
    _inherit = 'account.journal'

    LISTA_DTE = [
        ('33', 'Factura electrónica (33)'),
        ('34', 'Factura exenta electrónica (34)'),
        ('43', 'Liquidación factura electrónica (43)'),
        ('56', 'Nota de débito electrónica (56)'),
        ('61', 'Nota de crédito electrónica (61)'),
    ]

    config_dte_id = fields.Many2one('config.dte', string='Configuración DTE')
    cod_dte = fields.Selection(LISTA_DTE, string='Código de documento SII')
    secuencia_id = fields.Many2one('ir.sequence', string='Secuencia de folios SII')

    def _dte_sync_edi_format(self):
        edi_format = self.env.ref('tf_dte_cl.edi_format_tf_cl_dte', raise_if_not_found=False)
        if not edi_format:
            return
        for journal in self.filtered('cod_dte'):
            if edi_format not in journal.edi_format_ids:
                journal.edi_format_ids = [(4, edi_format.id)]

    @api.model_create_multi
    def create(self, vals_list):
        journals = super().create(vals_list)
        journals._dte_sync_edi_format()
        return journals

    def write(self, vals):
        res = super().write(vals)
        if 'cod_dte' in vals:
            self._dte_sync_edi_format()
        return res
