# -*- coding: utf-8 -*-
"""Catálogo de comunas de Chile (código INE).

Ruta real: models/res_comuna.py

Se puebla con ``hooks._tf_dte_cl_load_comunas`` desde data/res_comuna_seed.csv.
"""
from odoo import fields, models


class TfDteClComuna(models.Model):
    _name = 'tf_dte_cl.comuna'
    _description = 'Comuna de Chile'
    _order = 'name'
    _rec_names_search = ['name', 'code']

    name = fields.Char(string='Nombre', required=True, index=True)
    code = fields.Char(string='Código', required=True, index=True)
    country_id = fields.Many2one(
        'res.country', string='País', required=True, ondelete='restrict',
        default=lambda self: self.env.ref('base.cl', raise_if_not_found=False),
    )
    state_id = fields.Many2one(
        'res.country.state', string='Región', ondelete='restrict',
        domain="[('country_id', '=', country_id)]",
    )
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('code_country_uniq', 'unique(code, country_id)', 'Ya existe una comuna con ese código.'),
    ]
