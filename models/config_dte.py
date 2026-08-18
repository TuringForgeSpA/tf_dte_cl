# -*- coding: utf-8 -*-
from lxml import etree

from odoo import api, fields, models


class ActecosDte(models.Model):
    _name = 'actecos.dte'
    _description = 'Actividad económica SII'

    name = fields.Char(string='Código de acteco', size=64, required=True)
    glosa = fields.Char(string='Glosa', size=256, required=True)


class ConfigDte(models.Model):
    _name = 'config.dte'
    _description = 'Configuración DTE'

    LISTA_MODOS = [
        ('produccion', 'Producción'),
        ('certificacion', 'Certificación'),
        ('pruebas', 'Pruebas'),
    ]

    LISTA_FORMATOS = [
        ('a4a4', 'Documentos B2B en A4'),
        ('a4ter', 'Documentos B2B en formato térmico'),
    ]

    name = fields.Char(string='Razón social', size=64, required=True)
    rutemisor = fields.Char(string='RUT emisor', size=16, required=True)
    giroemisor = fields.Char(string='Giro', size=256, required=True)
    dirorigen = fields.Char(string='Dirección', size=256, required=True)
    cmnaorigen = fields.Char(string='Comuna', size=128, required=True)
    ciudadorigen = fields.Char(string='Ciudad', size=128, required=True)
    oficinasii = fields.Char(string='Oficina SII', size=256, required=True)
    correo = fields.Char(string='Correo')
    telefono = fields.Char(string='Teléfono')
    actecos = fields.Many2many('actecos.dte', string='Actividades económicas')
    modo = fields.Selection(LISTA_MODOS, string='Modo', default='pruebas', required=True)
    formato_impresion = fields.Selection(
        LISTA_FORMATOS, string='Formato de impresión', default='a4a4', required=True,
    )
    nroresol = fields.Char(string='Número de resolución', size=16, required=True)
    fchresol = fields.Char(string='Fecha de resolución', size=16, required=True)
    valoriva = fields.Char(string='Valor IVA', size=8, required=True)

    # Certificado gestionado (recomendado)
    firma_id = fields.Many2one('sii.firma', string='Certificado digital')
    # Campos de respaldo, solo si no se usa un certificado gestionado.
    rut_firmante = fields.Char(string='RUT del firmante', size=16)
    priv_key = fields.Text(string='Llave privada')
    cert = fields.Text(string='Certificado')

    caf_files_ids = fields.One2many('config.dte.caf', 'cd_id', string='Archivos CAF', copy=False)
    init_signature = fields.Selection([('F', 'No'), ('V', 'Sí')], string='Firma inicial', default='F')
    company_id = fields.Many2one(
        'res.company', string='Compañía', required=True, default=lambda self: self.env.company.id,
    )
    sucursal_ids = fields.One2many('config.sucursales', 'cd_id', string='Sucursales', copy=False)
    con_logo = fields.Boolean(string='DTE con logo')

    @api.constrains('firma_id', 'rut_firmante', 'priv_key', 'cert')
    def _check_firma_configurada(self):
        for conf in self:
            if not conf.firma_id and not (conf.rut_firmante and conf.priv_key and conf.cert):
                from odoo.exceptions import ValidationError
                raise ValidationError(
                    'Debe configurar un certificado digital (recomendado) o completar '
                    'manualmente el RUT del firmante, la llave privada y el certificado.'
                )


class ConfigDteCaf(models.Model):
    _name = 'config.dte.caf'
    _description = 'CAF (Código de autorización de folios)'

    LISTA_DTE = [
        ('33', 'Factura electrónica (33)'),
        ('34', 'Factura exenta electrónica (34)'),
        ('43', 'Liquidación factura electrónica (43)'),
        ('52', 'Guía de despacho electrónica (52)'),
        ('56', 'Nota de débito electrónica (56)'),
        ('61', 'Nota de crédito electrónica (61)'),
    ]

    name = fields.Selection(LISTA_DTE, string='Tipo de DTE')
    caf = fields.Text(string='CAF', required=True)
    rut = fields.Char(string='RUT', size=18)
    desde = fields.Char(string='Folio desde', size=16)
    hasta = fields.Char(string='Folio hasta', size=16)
    fecha = fields.Date(string='Fecha')
    cd_id = fields.Many2one('config.dte', string='Configuración DTE')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._completar_datos_caf(vals)
        return super().create(vals_list)

    def _completar_datos_caf(self, vals):
        if not vals.get('caf'):
            return vals
        tree = etree.fromstring(vals['caf'].encode()).find('CAF/DA')
        vals['name'] = tree.find('TD').text
        vals['rut'] = tree.find('RE').text
        vals['desde'] = tree.find('RNG/D').text
        vals['hasta'] = tree.find('RNG/H').text
        vals['fecha'] = tree.find('FA').text
        return vals


class ConfigSucursales(models.Model):
    _name = 'config.sucursales'
    _description = 'Sucursal'

    name = fields.Char(string='Dirección de la sucursal', size=256)
    state_id = fields.Many2one(
        'res.country.state', string='Región', ondelete='restrict', domain="[('country_id', '=?', country_id)]",
    )
    country_id = fields.Many2one('res.country', string='País', ondelete='restrict')
    cmna_id = fields.Many2one('res.comuna', string='Comuna', domain="[('state_id', '=', state_id)]")
    ciudad = fields.Char(string='Ciudad', size=128)
    cd_id = fields.Many2one('config.dte', string='Configuración DTE')
