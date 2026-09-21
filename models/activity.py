# -*- coding: utf-8 -*-
"""Actividades económicas (Acteco) informadas por el emisor.

Ruta real: models/activity.py

El catálogo del SII (data/sii_activities.csv) se carga con
``_tf_dte_cl_load_catalog``, que omite los códigos existentes: así no choca con
actividades importadas a mano ni con las que el usuario haya editado.
"""
import csv
import logging

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools.misc import file_open

_logger = logging.getLogger(__name__)

CATALOG_PATH = 'tf_dte_cl/data/sii_activities.csv'


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

    @api.model
    def _tf_dte_cl_load_catalog(self):
        """Crea las actividades del catálogo del SII que aún no existen (idempotente)."""
        existing = set(self.with_context(active_test=False).search([]).mapped('code'))
        vals_list = []
        with file_open(CATALOG_PATH, mode='r') as catalog:
            for row in csv.DictReader(catalog):
                code = (row.get('code') or '').strip()
                if code and code not in existing:
                    vals_list.append({'code': code, 'name': row['name'].strip()})
                    existing.add(code)
        if vals_list:
            self.create(vals_list)
        _logger.info('Catálogo de actividades económicas: %s actividad(es) creada(s).', len(vals_list))
