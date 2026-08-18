# -*- coding: utf-8 -*-
from odoo import fields, models


class ResComuna(models.Model):
    _name = 'res.comuna'
    _description = 'Comuna'
    _order = 'state_id, name'

    name = fields.Char(string='Nombre', required=True)
    codigo = fields.Char(string='Código')
    country_id = fields.Many2one('res.country', string='País', required=True)
    state_id = fields.Many2one(
        'res.country.state', string='Región', domain="[('country_id', '=', country_id)]",
    )
