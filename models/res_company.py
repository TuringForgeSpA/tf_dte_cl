# -*- coding: utf-8 -*-
"""Configuración del emisor DTE a nivel de compañía.

Ruta real: models/res_company.py

Reemplaza al antiguo modelo ``config.dte``: la compañía es el emisor (un RUT).
Razón social, RUT, dirección y ciudad se toman de la propia compañía; aquí solo
se agregan los datos que exige el SII y que Odoo no trae.
"""
from __future__ import annotations

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from .res_partner import is_valid_rut, normalize_rut

# DTE_v10.xsd, Encabezado/Emisor/Acteco: maxOccurs="4" (verificado en el xsd
# incluido en facturacion_electronica 0.24.0).
MAX_ACTIVITIES = 4


class ResCompany(models.Model):
    _inherit = 'res.company'

    tf_dte_cl_environment = fields.Selection(
        [('certificacion', 'Certificación'), ('produccion', 'Producción')],
        string='Ambiente SII', default='certificacion', required=True,
    )
    tf_dte_cl_resolution_number = fields.Integer(
        string='N° de resolución SII',
        help='En el ambiente de certificación corresponde 0.',
    )
    tf_dte_cl_resolution_date = fields.Date(string='Fecha de resolución SII')
    tf_dte_cl_activity_ids = fields.Many2many(
        'tf_dte_cl.activity', 'tf_dte_cl_company_activity_rel', 'company_id', 'activity_id',
        string='Actividades económicas',
    )
    tf_dte_cl_sii_office = fields.Char(
        string='Unidad regional SII',
        help='Texto impreso bajo el recuadro del folio, por ejemplo "S.I.I. - SANTIAGO CENTRO".',
    )
    tf_dte_cl_certificate_id = fields.Many2one(
        'tf_dte_cl.certificate', string='Certificado digital', ondelete='restrict',
        domain="[('company_id', '=', id)]",
    )
    tf_dte_cl_default_document = fields.Selection(
        [('33', 'Factura electrónica (33)'), ('34', 'Factura no afecta o exenta (34)')],
        string='Documento de venta por defecto', default='33', required=True,
    )
    tf_dte_cl_print_format = fields.Selection(
        [('a4', 'A4'), ('thermal', 'Térmico 80 mm')],
        string='Formato de impresión', default='a4', required=True,
    )
    tf_dte_cl_print_logo = fields.Boolean(string='Imprimir logo en el DTE', default=True)
    tf_dte_cl_print_cedible = fields.Boolean(
        string='Imprimir copia cedible', default=True,
        help='Agrega una segunda página con el acuse de recibo (Ley 19.983) en facturas y en '
             'guías que constituyen venta.',
    )
    tf_dte_cl_caf_validity_months = fields.Integer(
        string='Vigencia de CAF (meses)', default=6,
        help='Plazo desde la fecha de autorización tras el cual un CAF deja de usarse. '
             'Confirme el plazo vigente con la normativa del SII.',
    )
    tf_dte_cl_branch_ids = fields.One2many('tf_dte_cl.branch', 'company_id', string='Sucursales SII')
    tf_dte_cl_giro = fields.Char(related='partner_id.tf_dte_cl_giro', readonly=False)
    tf_dte_cl_comuna_id = fields.Many2one(
        related='partner_id.tf_dte_cl_comuna_id', readonly=False,
        domain="[('country_id.code', '=', 'CL')]",
    )
    tf_dte_cl_dte_email = fields.Char(related='partner_id.tf_dte_cl_dte_email', readonly=False)

    @api.constrains('tf_dte_cl_activity_ids')
    def _check_tf_dte_cl_activities(self):
        for company in self:
            if len(company.tf_dte_cl_activity_ids) > MAX_ACTIVITIES:
                raise ValidationError(self.env._(
                    'El DTE admite hasta %s actividades económicas por emisor.', MAX_ACTIVITIES,
                ))

    @api.constrains('tf_dte_cl_caf_validity_months')
    def _check_tf_dte_cl_caf_validity(self):
        for company in self:
            if company.tf_dte_cl_caf_validity_months < 1:
                raise ValidationError(self.env._('La vigencia de CAF debe ser de al menos un mes.'))

    @api.constrains('tf_dte_cl_certificate_id')
    def _check_tf_dte_cl_certificate_company(self):
        for company in self:
            cert = company.sudo().tf_dte_cl_certificate_id
            if cert and cert.company_id != company:
                raise ValidationError(self.env._('El certificado digital pertenece a otra compañía.'))

    # ------------------------------------------------------------------
    # Validación y datos del emisor
    # ------------------------------------------------------------------
    def _tf_dte_cl_emitter_errors(self) -> list[str]:
        """Motivos por los que la compañía no puede emitir DTE (lista vacía si puede)."""
        self.ensure_one()
        _ = self.env._
        errors = []
        if not is_valid_rut(self.vat):
            errors.append(_('RUT de la compañía inválido o vacío'))
        required = (
            (self.name, _('razón social')),
            (self.tf_dte_cl_giro, _('giro')),
            (self.street, _('dirección')),
            (self.tf_dte_cl_comuna_id, _('comuna')),
            (self.city, _('ciudad')),
            (self.tf_dte_cl_dte_email or self.email, _('correo del emisor')),
            (self.tf_dte_cl_activity_ids, _('al menos una actividad económica')),
            (self.tf_dte_cl_resolution_date, _('fecha de resolución SII')),
        )
        errors += [_('falta %s', label) for value, label in required if not value]
        if self.currency_id.name != 'CLP':
            errors.append(_('la moneda de la compañía debe ser CLP'))
        if self.tax_calculation_rounding_method != 'round_globally':
            errors.append(_(
                'el redondeo de impuestos debe ser global, porque el IVA del DTE se calcula sobre el total'
            ))
        errors += self.sudo().tf_dte_cl_certificate_id._tf_dte_cl_usability_errors()
        return errors

    def _tf_dte_cl_check_emitter(self) -> None:
        errors = self._tf_dte_cl_emitter_errors()
        if errors:
            raise UserError(self.env._(
                'La configuración de facturación electrónica de "%(company)s" está incompleta:\n- %(errors)s',
                company=self.name, errors='\n- '.join(errors),
            ))

    def _tf_dte_cl_emitter_payload(self, branch=None) -> dict:
        """Bloque ``Emisor`` para facturacion_electronica."""
        self.ensure_one()
        self._tf_dte_cl_check_emitter()
        payload = {
            'RUTEmisor': normalize_rut(self.vat),
            'RznSoc': self.name,
            'GiroEmis': self.tf_dte_cl_giro,
            'Telefono': self.phone,
            'CorreoEmisor': self.tf_dte_cl_dte_email or self.email,
            'Actecos': [int(activity.code) for activity in self.tf_dte_cl_activity_ids],
            'DirOrigen': ', '.join(filter(None, [self.street, self.street2])),
            'CmnaOrigen': self.tf_dte_cl_comuna_id.name,
            'CiudadOrigen': self.city,
            'Modo': self.tf_dte_cl_environment,
            'NroResol': self.tf_dte_cl_resolution_number or 0,
            'FchResol': self.tf_dte_cl_resolution_date,
            'Website': self.website,
        }
        if branch:
            if branch.company_id != self:
                raise UserError(self.env._('La sucursal "%s" pertenece a otra compañía.', branch.name))
            payload.update(branch._tf_dte_cl_origin_payload())
        return payload

    def _tf_dte_cl_signature_payload(self) -> dict:
        """Bloque ``firma_electronica`` para facturacion_electronica."""
        self.ensure_one()
        return self.sudo().tf_dte_cl_certificate_id._tf_dte_cl_signature_payload()
