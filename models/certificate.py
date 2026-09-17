# -*- coding: utf-8 -*-
"""Certificado digital (PKCS#12) usado para firmar DTE.

Ruta real: models/certificate.py

El .p12 se abre una sola vez al cargarlo, con ``cryptography`` (la misma vía que
usa ``facturacion_electronica.signature_cert``). Se guardan la llave y el
certificado en PEM, en campos visibles solo para ``base.group_system``; el
archivo y la contraseña no se conservan.

Sobre OpenSSL 3 y .p12 antiguos (RC2-40-CBC): los wheels de ``cryptography``
cargan el proveedor "legacy" de OpenSSL por defecto (salvo que se defina
``CRYPTOGRAPHY_OPENSSL_NO_LEGACY``). Si ``cryptography`` está compilado contra
un OpenSSL sin ese proveedor, la carga falla y se informa al usuario; el
archivo nunca se reexporta en silencio.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta, timezone

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from .res_partner import is_valid_rut, normalize_rut

_logger = logging.getLogger(__name__)

try:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, pkcs12,
    )
    from cryptography.x509.oid import NameOID
except ImportError:  # pragma: no cover - cryptography es dependencia de Odoo 18
    pkcs12 = None

EXPIRY_WARNING_DAYS = 30


def _name_attribute(name, oid) -> str | bool:
    values = name.get_attributes_for_oid(oid)
    return values[0].value if values else False


def _utc_naive(cert, attribute: str) -> datetime:
    """Fecha del certificado en UTC sin zona (formato de fields.Datetime)."""
    value = getattr(cert, attribute + '_utc', None)  # cryptography >= 42
    if value is None:
        value = getattr(cert, attribute).replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _password_candidates(password: str) -> list[bytes | None]:
    if not password:
        return [None, b'']
    candidates = [password.encode('utf-8')]
    try:
        latin = password.encode('latin-1')
    except UnicodeEncodeError:
        latin = None
    if latin and latin not in candidates:
        candidates.append(latin)
    return candidates


class TfDteClCertificate(models.Model):
    _name = 'tf_dte_cl.certificate'
    _description = 'Certificado digital SII'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date_end desc, id desc'
    _check_company_auto = True

    name = fields.Char(string='Nombre', required=True, tracking=True)
    company_id = fields.Many2one(
        'res.company', string='Compañía', required=True, index=True,
        default=lambda self: self.env.company,
    )
    active = fields.Boolean(default=True, tracking=True)
    responsible_id = fields.Many2one(
        'res.users', string='Responsable', default=lambda self: self.env.user,
        help='Usuario que recibe la alerta de vencimiento.',
    )
    signer_rut = fields.Char(
        string='RUT del firmante', tracking=True,
        help='Se toma del certificado. Si el certificado no lo informa, ingréselo manualmente.',
    )
    subject_name = fields.Char(string='Titular', readonly=True)
    subject_email = fields.Char(string='Correo del titular', readonly=True)
    issuer_name = fields.Char(string='Entidad certificadora', readonly=True)
    serial_number = fields.Char(string='Número de serie', readonly=True)
    date_start = fields.Datetime(string='Válido desde', readonly=True)
    date_end = fields.Datetime(string='Válido hasta', readonly=True, tracking=True)
    state = fields.Selection(
        [
            ('valid', 'Vigente'),
            ('expiring', 'Por vencer'),
            ('expired', 'Vencido'),
            ('not_yet', 'Aún no vigente'),
        ],
        string='Estado', compute='_compute_state',
    )
    has_key = fields.Boolean(string='Llave cargada', compute='_compute_has_key')
    certificate_pem = fields.Text(
        string='Certificado (PEM)', readonly=True, copy=False, groups='base.group_system',
    )
    private_key_pem = fields.Text(
        string='Llave privada (PEM)', readonly=True, copy=False, groups='base.group_system',
    )

    _sql_constraints = [
        ('serial_issuer_company_uniq', 'unique(company_id, issuer_name, serial_number)',
         'Este certificado ya está cargado en la compañía.'),
    ]

    # ------------------------------------------------------------------
    # Cómputos y restricciones
    # ------------------------------------------------------------------
    @api.depends('date_start', 'date_end')
    def _compute_state(self):
        now = fields.Datetime.now()
        warning_limit = now + timedelta(days=EXPIRY_WARNING_DAYS)
        for cert in self:
            if cert.date_start and cert.date_start > now:
                cert.state = 'not_yet'
            elif not cert.date_end or cert.date_end <= now:
                cert.state = 'expired'
            elif cert.date_end <= warning_limit:
                cert.state = 'expiring'
            else:
                cert.state = 'valid'

    @api.depends('date_end')
    def _compute_has_key(self):
        for cert in self:
            cert.has_key = bool(cert.sudo().private_key_pem)

    @api.constrains('signer_rut')
    def _check_signer_rut(self):
        for cert in self:
            if cert.signer_rut and not is_valid_rut(cert.signer_rut):
                raise ValidationError(self.env._('RUT del firmante inválido: %s.', cert.signer_rut))

    def write(self, vals):
        if vals.get('signer_rut'):
            vals = dict(vals, signer_rut=normalize_rut(vals['signer_rut']) or vals['signer_rut'])
        return super().write(vals)

    # ------------------------------------------------------------------
    # Carga del PKCS#12
    # ------------------------------------------------------------------
    @api.model
    def _tf_dte_cl_parse_p12(self, data: bytes, password: str) -> dict:
        """Abre un .p12/.pfx y devuelve los valores para crear el certificado."""
        if pkcs12 is None:
            raise UserError(self.env._('Falta la librería "cryptography" en el servidor.'))
        loaded = None
        last_error = None
        for candidate in _password_candidates(password):
            try:
                loaded = pkcs12.load_key_and_certificates(data, candidate)
                break
            except ValueError as error:
                last_error = error
            except Exception as error:  # noqa: BLE001 - algoritmo no soportado u otro error de OpenSSL
                last_error = error
                break
        if loaded is None:
            # El mensaje de cryptography no contiene la contraseña ni la llave.
            _logger.info('No se pudo abrir un certificado PKCS#12: %s', type(last_error).__name__)
            raise UserError(self.env._(
                'No se pudo abrir el certificado. Verifique la contraseña. Si es correcta, el '
                'archivo puede usar cifrado antiguo (RC2-40-CBC) y el servidor no tiene habilitado '
                'el proveedor "legacy" de OpenSSL 3; en ese caso, habilítelo en el servidor.'
            ))
        key, cert, _additional = loaded
        if key is None or cert is None:
            raise UserError(self.env._('El archivo no contiene la llave privada y el certificado.'))
        if not isinstance(key, rsa.RSAPrivateKey):
            raise UserError(self.env._('La firma de DTE requiere una llave RSA.'))
        serial_attr = _name_attribute(cert.subject, NameOID.SERIAL_NUMBER)
        return {
            'subject_name': _name_attribute(cert.subject, NameOID.COMMON_NAME),
            'subject_email': _name_attribute(cert.subject, NameOID.EMAIL_ADDRESS),
            'issuer_name': _name_attribute(cert.issuer, NameOID.COMMON_NAME),
            'serial_number': format(cert.serial_number, 'X'),
            'date_start': _utc_naive(cert, 'not_valid_before'),
            'date_end': _utc_naive(cert, 'not_valid_after'),
            'signer_rut': normalize_rut(serial_attr) if is_valid_rut(serial_attr) else False,
            'certificate_pem': cert.public_bytes(Encoding.PEM).decode('ascii'),
            'private_key_pem': key.private_bytes(
                Encoding.PEM, PrivateFormat.PKCS8, NoEncryption(),
            ).decode('ascii'),
        }

    # ------------------------------------------------------------------
    # Uso en la firma
    # ------------------------------------------------------------------
    def _tf_dte_cl_usability_errors(self) -> list[str]:
        """Motivos por los que el certificado no puede firmar (lista vacía si puede)."""
        if not self:
            return [self.env._('no hay un certificado digital configurado')]
        self.ensure_one()
        cert = self.sudo()
        errors = []
        if not cert.active:
            errors.append(self.env._('el certificado "%s" está archivado', cert.name))
        if not cert.private_key_pem or not cert.certificate_pem:
            errors.append(self.env._('el certificado "%s" no tiene llave cargada', cert.name))
        if cert.state == 'expired':
            errors.append(self.env._('el certificado "%s" está vencido', cert.name))
        elif cert.state == 'not_yet':
            errors.append(self.env._('el certificado "%s" aún no está vigente', cert.name))
        if not is_valid_rut(cert.signer_rut):
            errors.append(self.env._('el certificado "%s" no tiene un RUT de firmante válido', cert.name))
        return errors

    def _tf_dte_cl_signature_payload(self) -> dict:
        """Datos de firma para facturacion_electronica (forma PEM)."""
        self.ensure_one()
        errors = self._tf_dte_cl_usability_errors()
        if errors:
            raise UserError(self.env._('No se puede firmar: %s.', '; '.join(errors)))
        cert = self.sudo()
        return {
            'priv_key': cert.private_key_pem,
            'cert': cert.certificate_pem,
            'rut_firmante': normalize_rut(cert.signer_rut),
        }

    # ------------------------------------------------------------------
    # Cron de alerta de vencimiento
    # ------------------------------------------------------------------
    @api.model
    def _cron_tf_dte_cl_expiry_alert(self):
        limit = fields.Datetime.now() + timedelta(days=EXPIRY_WARNING_DAYS)
        activity_type = self.env.ref('mail.mail_activity_data_warning', raise_if_not_found=False)
        certificates = self.sudo().search([('date_end', '<=', limit)])
        for cert in certificates:
            if activity_type and cert.activity_ids.filtered(lambda a: a.activity_type_id == activity_type):
                continue
            cert.activity_schedule(
                'mail.mail_activity_data_warning',
                date_deadline=cert.date_end.date(),
                summary=self.env._('Certificado digital por vencer'),
                note=self.env._(
                    'El certificado "%(name)s" de %(company)s vence el %(date)s. '
                    'Cargue uno nuevo para no interrumpir la emisión de DTE.',
                    name=cert.name, company=cert.company_id.name, date=cert.date_end.date(),
                ),
                user_id=(cert.responsible_id or self.env.ref('base.user_admin')).id,
            )


class TfDteClCertificateLoad(models.TransientModel):
    _name = 'tf_dte_cl.certificate.load'
    _description = 'Carga de certificado digital'

    company_id = fields.Many2one(
        'res.company', string='Compañía', required=True, default=lambda self: self.env.company,
    )
    # Sin required a nivel de modelo: la columna quedaría NOT NULL y no podría
    # vaciarse al terminar la carga. La obligatoriedad se exige en la vista y en action_load.
    p12_file = fields.Binary(string='Archivo (.p12 / .pfx)', attachment=False)
    p12_filename = fields.Char(string='Nombre del archivo')
    password = fields.Char(string='Contraseña')
    make_default = fields.Boolean(string='Usar como certificado de la compañía', default=True)

    def _tf_dte_cl_clear_secrets_now(self):
        """Borra archivo y contraseña en una transacción propia.

        Un UserError revierte la transacción actual; sin esto, la contraseña
        quedaría en la tabla del asistente hasta la limpieza automática.
        """
        if not self.ids:
            return
        with self.env.registry.cursor() as cr:
            cr.execute(
                'UPDATE tf_dte_cl_certificate_load SET p12_file = NULL, password = NULL WHERE id IN %s',
                [tuple(self.ids)],
            )

    def action_load(self):
        self.ensure_one()
        if self.company_id not in self.env.companies:
            raise UserError(self.env._('No tiene acceso a la compañía seleccionada.'))
        if not self.p12_file:
            raise UserError(self.env._('Debe adjuntar el archivo del certificado (.p12 o .pfx).'))
        Certificate = self.env['tf_dte_cl.certificate']
        try:
            vals = Certificate._tf_dte_cl_parse_p12(base64.b64decode(self.p12_file), self.password or '')
        except UserError as error:
            self._tf_dte_cl_clear_secrets_now()
            raise UserError(self.env._(
                '%s\nVuelva a abrir el asistente para intentarlo nuevamente.', error.args[0],
            )) from None
        self.sudo().write({'p12_file': False, 'password': False})
        vals.update({
            'name': self.p12_filename or vals['subject_name'] or self.env._('Certificado digital'),
            'company_id': self.company_id.id,
        })
        # sudo: la llave y el certificado solo son escribibles por base.group_system.
        certificate = Certificate.sudo().create(vals)
        if self.make_default:
            self.company_id.sudo().tf_dte_cl_certificate_id = certificate
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'tf_dte_cl.certificate',
            'res_id': certificate.id,
            'view_mode': 'form',
            'target': 'current',
        }
