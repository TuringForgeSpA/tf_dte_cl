# -*- coding: utf-8 -*-
"""Código SII de los impuestos de venta.

Ruta real: models/account_tax.py
"""
from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .dte_lines import SII_TAX_SELECTION, TaxInfo, tax_config_errors


class AccountTax(models.Model):
    _inherit = 'account.tax'

    tf_dte_cl_sii_code = fields.Selection(
        SII_TAX_SELECTION, string='Código SII',
        help='Código con el que se informa el impuesto en el DTE. Los impuestos de venta usados en '
             'documentos electrónicos deben tenerlo; los precios se informan netos.',
    )

    def _tf_dte_cl_info(self) -> TaxInfo:
        self.ensure_one()
        return TaxInfo(
            name=self.name,
            code=self.tf_dte_cl_sii_code or None,
            rate=self.amount,
            amount_type=self.amount_type,
            price_include=bool(self.price_include),
            include_base_amount=bool(self.include_base_amount),
            type_tax_use=self.type_tax_use,
        )

    @api.constrains('tf_dte_cl_sii_code', 'amount', 'amount_type', 'type_tax_use', 'include_base_amount')
    def _check_tf_dte_cl_sii_code(self):
        for tax in self.filtered('tf_dte_cl_sii_code'):
            errors = tax_config_errors(tax._tf_dte_cl_info())
            if errors:
                raise ValidationError(self.env._(
                    'El impuesto "%(tax)s" no es válido para el SII: %(errors)s.',
                    tax=tax.name, errors='; '.join(errors),
                ))
