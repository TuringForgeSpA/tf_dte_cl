# -*- coding: utf-8 -*-
from odoo import fields, models


class AccountTax(models.Model):
    _inherit = 'account.tax'

    codigo_sii = fields.Integer(
        string='Código SII', help='Código utilizado por el SII para identificar el impuesto.',
    )
