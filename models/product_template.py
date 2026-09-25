# -*- coding: utf-8 -*-
"""Productos de descuento o recargo global.

Ruta real: models/product_template.py

Una línea de factura con uno de estos productos no va al detalle del DTE: se
informa como descuento (precio negativo) o recargo (positivo) global en pesos,
afecto si la línea lleva IVA y exento si no lleva impuestos.
"""
from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    tf_dte_cl_global_adjustment = fields.Boolean(
        string='Descuento o recargo global DTE',
        help='En las facturas, las líneas con este producto se informan al SII como descuento '
             '(precio negativo) o recargo (precio positivo) global, no como una línea de detalle. '
             'Con IVA es afecto; sin impuestos, exento.',
    )
