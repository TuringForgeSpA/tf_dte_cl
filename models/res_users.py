# -*- coding: utf-8 -*-
from odoo import fields, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    sucursal_id = fields.Many2one('config.sucursales', string='Sucursal')
