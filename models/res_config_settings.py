# -*- coding: utf-8 -*-
"""Ajustes de facturación electrónica (Contabilidad > Ajustes).

Ruta real: models/res_config_settings.py
"""
from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    tf_dte_cl_environment = fields.Selection(related='company_id.tf_dte_cl_environment', readonly=False)
    tf_dte_cl_resolution_number = fields.Integer(
        related='company_id.tf_dte_cl_resolution_number', readonly=False,
    )
    tf_dte_cl_resolution_date = fields.Date(related='company_id.tf_dte_cl_resolution_date', readonly=False)
    tf_dte_cl_activity_ids = fields.Many2many(related='company_id.tf_dte_cl_activity_ids', readonly=False)
    tf_dte_cl_sii_office = fields.Char(related='company_id.tf_dte_cl_sii_office', readonly=False)
    tf_dte_cl_certificate_id = fields.Many2one(
        related='company_id.tf_dte_cl_certificate_id', readonly=False,
        domain="[('company_id', '=', company_id)]",
    )
    tf_dte_cl_default_document = fields.Selection(
        related='company_id.tf_dte_cl_default_document', readonly=False,
    )
    tf_dte_cl_print_format = fields.Selection(related='company_id.tf_dte_cl_print_format', readonly=False)
    tf_dte_cl_print_logo = fields.Boolean(related='company_id.tf_dte_cl_print_logo', readonly=False)
    tf_dte_cl_print_cedible = fields.Boolean(related='company_id.tf_dte_cl_print_cedible', readonly=False)
    tf_dte_cl_caf_validity_months = fields.Integer(
        related='company_id.tf_dte_cl_caf_validity_months', readonly=False,
    )
    tf_dte_cl_giro = fields.Char(related='company_id.tf_dte_cl_giro', readonly=False)
    tf_dte_cl_comuna_id = fields.Many2one(
        related='company_id.tf_dte_cl_comuna_id', readonly=False,
        domain="[('country_id.code', '=', 'CL')]",
    )
    tf_dte_cl_dte_email = fields.Char(related='company_id.tf_dte_cl_dte_email', readonly=False)
    tf_dte_cl_unknown_resend_minutes = fields.Integer(
        string='Espera antes de reenviar (minutos)', default=60,
        config_parameter='tf_dte_cl.unknown_resend_minutes',
        help='Si no se sabe si un envío llegó al SII, primero se consulta; solo se reenvía el '
             'mismo sobre cuando el SII confirma que no lo recibió y pasó este tiempo.',
    )
    tf_dte_cl_emitter_errors = fields.Text(compute='_compute_tf_dte_cl_emitter_errors')

    @api.depends(
        'company_id', 'tf_dte_cl_environment', 'tf_dte_cl_resolution_date', 'tf_dte_cl_activity_ids',
        'tf_dte_cl_certificate_id', 'tf_dte_cl_giro', 'tf_dte_cl_comuna_id', 'tf_dte_cl_dte_email',
    )
    def _compute_tf_dte_cl_emitter_errors(self):
        # Refleja lo guardado en la compañía; los cambios sin guardar se ven al presionar "Guardar".
        for settings in self:
            errors = settings.company_id._tf_dte_cl_emitter_errors()
            settings.tf_dte_cl_emitter_errors = '\n'.join('• %s' % error for error in errors)
