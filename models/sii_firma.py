# -*- coding: utf-8 -*-
import base64
import logging
from datetime import datetime

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

try:
    from OpenSSL import crypto
    TIPO_PEM = crypto.FILETYPE_PEM
except ImportError:
    crypto = None
    _logger.warning('No se pudo importar "OpenSSL". Instálelo con: pip install pyOpenSSL')

try:
    from facturacion_electronica.firma import Firma
except ImportError:
    Firma = None
    _logger.warning(
        'No se pudo importar la librería "facturacion_electronica". '
        'Instálela con: pip install facturacion_electronica'
    )


class SiiFirma(models.Model):
    _name = 'sii.firma'
    _description = 'Certificado digital para firma electrónica SII'
    _order = 'priority desc'

    name = fields.Char(string='Nombre del archivo', required=True)
    file_content = fields.Binary(string='Archivo de firma (.p12)')
    password = fields.Char(string='Contraseña')
    emision_date = fields.Date(string='Fecha de emisión', readonly=True)
    expire_date = fields.Date(string='Fecha de vencimiento', readonly=True)
    state = fields.Selection(
        [
            ('unverified', 'Sin verificar'),
            ('incomplete', 'Incompleto'),
            ('valid', 'Vigente'),
            ('expired', 'Vencido'),
        ],
        string='Estado', default='unverified',
        help='Borrador: aún no se ha verificado. Debe presionar el botón "Procesar".',
    )
    subject_common_name = fields.Char(string='Titular', readonly=True)
    subject_serial_number = fields.Char(string='RUT del firmante', readonly=True)
    subject_email_address = fields.Char(string='Correo del titular', readonly=True)
    issuer_common_name = fields.Char(string='Entidad certificadora', readonly=True)
    cert_serial_number = fields.Char(string='Número de serie', readonly=True)
    cert = fields.Text(string='Certificado', readonly=True)
    priv_key = fields.Text(string='Llave privada', readonly=True)
    user_ids = fields.Many2many('res.users', string='Usuarios autorizados', default=lambda self: [self.env.uid])
    company_ids = fields.Many2many(
        'res.company', string='Compañías autorizadas',
        default=lambda self: [self.env.company.id], required=True,
    )
    priority = fields.Integer(string='Prioridad', default=1)
    active = fields.Boolean(string='Activo', default=True)

    _sql_constraints = [
        ('name_uniq', 'unique(name, subject_serial_number, active)', '¡El nombre debe ser único!'),
    ]

    @api.onchange('subject_serial_number')
    def _onchange_subject_serial_number(self):
        if self.subject_serial_number:
            partner = self.env.user.partner_id
            rut = self.subject_serial_number.replace('.', '').upper()
            if len(rut) == 9:
                rut = '0' + rut
            if '-' not in rut or not partner.check_vat_cl(rut.replace('-', '')):
                raise UserError(_('El RUT del firmante no es válido.'))
            self.subject_serial_number = rut
        elif self.file_content:
            self.state = 'incomplete'

    def alerta_vencimiento(self):
        for firma in self:
            if firma.expire_date and firma.expire_date < (fields.Date.today() + relativedelta(days=30)):
                self.env['bus.bus']._sendone(
                    self.env.user.partner_id, 'dte_notif',
                    {'title': _('Alerta de firma electrónica'), 'message': _('La firma "%s" está próxima a vencer.') % firma.name},
                )

    def check_signature(self):
        for firma in self.sudo():
            vencido = firma.expire_date and firma.expire_date < fields.Date.context_today(self)
            estado = 'expired' if vencido else 'valid'
            if firma.state != estado:
                firma.write({'state': estado, 'active': not vencido})

    def action_process(self):
        self.ensure_one()
        if crypto is None:
            raise UserError(_('No está disponible la librería "OpenSSL" en el servidor.'))
        if self.subject_serial_number:
            return self.check_signature()
        if not self.file_content:
            raise UserError(_('Debe adjuntar el archivo de firma electrónica (.p12).'))
        try:
            p12 = crypto.load_pkcs12(base64.b64decode(self.file_content), self.password)
        except Exception:
            raise UserError(_(
                'No se pudo abrir el archivo de firma. Verifique que la contraseña sea '
                'correcta y que el archivo sea compatible.'
            ))
        cert = p12.get_certificate()
        subject = cert.get_subject()
        issuer = cert.get_issuer()
        self.write({
            'emision_date': datetime.strptime(cert.get_notBefore().decode(), '%Y%m%d%H%M%SZ'),
            'expire_date': datetime.strptime(cert.get_notAfter().decode(), '%Y%m%d%H%M%SZ'),
            'subject_common_name': subject.CN,
            'subject_serial_number': subject.serialNumber,
            'subject_email_address': subject.emailAddress,
            'issuer_common_name': issuer.CN,
            'cert_serial_number': cert.get_serial_number(),
            'priv_key': crypto.dump_privatekey(TIPO_PEM, p12.get_privatekey()),
            'cert': crypto.dump_certificate(TIPO_PEM, cert),
            'password': False,
        })
        self.check_signature()

    def parametros_firma(self):
        self.ensure_one()
        return {
            'priv_key': self.priv_key,
            'cert': self.cert,
            'rut_firmante': self.subject_serial_number,
            'init_signature': False,
        }

    def firmar(self, string, uri=False, type='doc'):
        self.ensure_one()
        if Firma is None:
            raise UserError(_('No está disponible la librería "facturacion_electronica" en el servidor.'))
        return Firma(self.parametros_firma()).firmar(string=string, uri=uri, type=type)
