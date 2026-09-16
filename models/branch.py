# -*- coding: utf-8 -*-
"""Sucursales informadas en el DTE (Sucursal / CdgSIISucur).

Ruta real: models/branch.py
"""
from odoo import api, fields, models
from odoo.exceptions import ValidationError

# CdgSIISucur: la librería lo corta a 9 caracteres y exige texto.
MAX_SII_CODE_LENGTH = 9


class TfDteClBranch(models.Model):
    _name = 'tf_dte_cl.branch'
    _description = 'Sucursal SII'
    _order = 'company_id, name'
    _check_company_auto = True

    name = fields.Char(
        string='Nombre', required=True,
        help='Nombre de la sucursal. En el DTE se informan hasta 20 caracteres.',
    )
    sii_code = fields.Char(
        string='Código SII', required=True,
        help='Código numérico asignado por el SII a la sucursal.',
    )
    company_id = fields.Many2one(
        'res.company', string='Compañía', required=True, index=True,
        default=lambda self: self.env.company,
    )
    street = fields.Char(string='Dirección')
    city = fields.Char(string='Ciudad')
    comuna_id = fields.Many2one('tf_dte_cl.comuna', string='Comuna', ondelete='restrict')
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('company_code_uniq', 'unique(company_id, sii_code)',
         'Ya existe una sucursal con ese código SII en la compañía.'),
    ]

    @api.constrains('sii_code')
    def _check_sii_code(self):
        for branch in self:
            code = branch.sii_code or ''
            if not code.isdigit() or len(code) > MAX_SII_CODE_LENGTH:
                raise ValidationError(self.env._(
                    'El código SII de la sucursal debe ser numérico y de hasta %(max)s dígitos: %(code)s.',
                    max=MAX_SII_CODE_LENGTH, code=code,
                ))

    def _tf_dte_cl_origin_payload(self) -> dict:
        """Datos de origen del emisor para el DTE. La dirección solo reemplaza a la
        de la compañía si la sucursal la tiene completa."""
        self.ensure_one()
        payload = {'Sucursal': self.name, 'CdgSIISucur': str(self.sii_code)}
        if self.street and self.comuna_id and self.city:
            payload.update({
                'DirOrigen': self.street,
                'CmnaOrigen': self.comuna_id.name,
                'CiudadOrigen': self.city,
            })
        return payload
