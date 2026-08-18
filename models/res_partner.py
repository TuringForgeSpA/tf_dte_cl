# -*- coding: utf-8 -*-
from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    comuna_id = fields.Many2one('res.comuna', string='Comuna', domain="[('state_id', '=', state_id)]")
    giro = fields.Char(string='Giro', size=256)
    transportista = fields.Boolean(string='Es transportista', default=False)
