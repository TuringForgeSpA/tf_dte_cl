# -*- coding: utf-8 -*-
"""Actividades económicas (Acteco) informadas por el emisor.

Ruta real: models/activity.py
"""
from odoo import api, fields, models
from odoo.exceptions import ValidationError


class TfDteClActivity(models.Model):
    _name = 'tf_dte_cl.activity'
    _description = 'Actividad económica SII (Acteco)'
    _order = 'code'
    _rec_names_search = ['code', 'name']

    code = fields.Char(string='Código', required=True, index=True)
    name = fields.Char(string='Glosa', required=True)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('code_uniq', 'unique(code)', 'Ya existe una actividad económica con ese código.'),
    ]

    @api.depends('code', 'name')
    def _compute_display_name(self):
        for activity in self:
            activity.display_name = '%s - %s' % (activity.code, activity.name) if activity.code else activity.name

    @api.constrains('code')
    def _check_code(self):
        for activity in self:
            if not (activity.code or '').isdigit():
                raise ValidationError(self.env._(
                    'El código de actividad económica debe ser numérico: %s.', activity.code,
                ))
