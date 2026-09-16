# -*- coding: utf-8 -*-
"""Datos tributarios chilenos del contacto y utilidades de RUT.

Ruta real: models/res_partner.py

La librería facturacion_electronica no valida el dígito verificador del RUT
(``clase_util.verificar_rut`` es un TODO) y reemplaza un receptor vacío por
66666666-6, por lo que la validación vive aquí.
"""
from __future__ import annotations

import re

from odoo import api, fields, models
from odoo.exceptions import ValidationError

SII_RUT = '60803000-K'
GENERIC_RUTS = frozenset({'66666666-6', '55555555-5'})

_RUT_RE = re.compile(r'^(\d{1,10})-?([\dK])$')


def normalize_rut(value) -> str | bool:
    """Normaliza un RUT al formato del SII: sin puntos, con guion y K mayúscula.

    ``'76.086.428-5'``, ``'CL760864285'`` → ``'76086428-5'``.
    Devuelve ``False`` si el texto no tiene forma de RUT.
    """
    if not value:
        return False
    text = re.sub(r'[\s.]', '', str(value)).upper()
    if text.startswith('CL'):
        text = text[2:]
    match = _RUT_RE.match(text)
    if not match:
        return False
    body = match.group(1).lstrip('0') or '0'
    if len(body) > 8:
        return False
    return '%s-%s' % (body, match.group(2))


def rut_check_digit(body: str) -> str:
    """Dígito verificador (módulo 11) de la parte numérica del RUT."""
    total = sum(int(digit) * (2 + index % 6) for index, digit in enumerate(reversed(body)))
    digit = 11 - total % 11
    return {11: '0', 10: 'K'}.get(digit, str(digit))


def is_valid_rut(value) -> bool:
    rut = normalize_rut(value)
    if not rut:
        return False
    body, digit = rut.split('-')
    return body != '0' and rut_check_digit(body) == digit


class ResPartner(models.Model):
    _inherit = 'res.partner'

    tf_dte_cl_comuna_id = fields.Many2one(
        'tf_dte_cl.comuna', string='Comuna', ondelete='restrict',
        domain="[('country_id', '=?', country_id), ('state_id', '=?', state_id)]",
    )
    tf_dte_cl_giro = fields.Char(
        string='Giro',
        help='Glosa del giro comercial. En el DTE del receptor se informan hasta 40 caracteres.',
    )
    tf_dte_cl_dte_email = fields.Char(
        string='Correo de intercambio DTE',
        help='Casilla registrada ante el SII para recibir documentos tributarios electrónicos.',
    )
    tf_dte_cl_is_carrier = fields.Boolean(string='Es transportista')

    # ------------------------------------------------------------------
    # Sincronización con contactos hijos
    # ------------------------------------------------------------------
    @api.model
    def _commercial_fields(self):
        return super()._commercial_fields() + ['tf_dte_cl_giro', 'tf_dte_cl_dte_email']

    @api.model
    def _address_fields(self):
        return super()._address_fields() + ['tf_dte_cl_comuna_id']

    @api.onchange('tf_dte_cl_comuna_id')
    def _onchange_tf_dte_cl_comuna_id(self):
        comuna = self.tf_dte_cl_comuna_id
        if comuna.country_id:
            self.country_id = comuna.country_id
        if comuna.state_id:
            self.state_id = comuna.state_id

    # ------------------------------------------------------------------
    # RUT
    # ------------------------------------------------------------------
    def _tf_dte_cl_normalize_vat_vals(self, vals: dict) -> None:
        """Normaliza ``vals['vat']`` solo para contactos chilenos."""
        if not vals.get('vat'):
            return
        if vals.get('country_id'):
            is_chilean = self.env['res.country'].browse(vals['country_id']).code == 'CL'
        else:
            is_chilean = bool(self) and all(partner.country_id.code == 'CL' for partner in self)
        rut = is_chilean and normalize_rut(vals['vat'])
        if rut:
            vals['vat'] = rut

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self.browse()._tf_dte_cl_normalize_vat_vals(vals)
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('vat'):
            vals = dict(vals)
            self._tf_dte_cl_normalize_vat_vals(vals)
        return super().write(vals)

    @api.constrains('vat', 'country_id')
    def _check_tf_dte_cl_vat(self):
        for partner in self:
            if partner.vat and partner.country_id.code == 'CL' and not is_valid_rut(partner.vat):
                raise ValidationError(self.env._(
                    'RUT inválido para "%(name)s": %(vat)s.',
                    name=partner.display_name, vat=partner.vat,
                ))

    def _tf_dte_cl_receiver_errors(self) -> list[str]:
        """Datos obligatorios del receptor de un DTE B2B que faltan o son inválidos."""
        self.ensure_one()
        partner = self.commercial_partner_id
        errors = []
        rut = normalize_rut(partner.vat)
        if not is_valid_rut(rut):
            errors.append(self.env._('RUT inválido o vacío'))
        elif rut in GENERIC_RUTS:
            errors.append(self.env._('RUT genérico (%s) no permitido en documentos B2B', rut))
        checks = (
            (partner.name, self.env._('razón social')),
            (partner.tf_dte_cl_giro, self.env._('giro')),
            (self.street, self.env._('dirección')),
            (self.tf_dte_cl_comuna_id, self.env._('comuna')),
            (self.city, self.env._('ciudad')),
        )
        errors += [self.env._('falta %s', label) for value, label in checks if not value]
        return errors
