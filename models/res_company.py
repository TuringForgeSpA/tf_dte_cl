# -*- coding: utf-8 -*-
from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    doc_defecto = fields.Char(string='Documento DTE por defecto')
