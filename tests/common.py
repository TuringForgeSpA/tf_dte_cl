# -*- coding: utf-8 -*-
"""Datos de prueba y simulación del SII para las pruebas de tf_dte_cl.

Ruta real: tests/common.py

- El certificado y los CAF se generan al vuelo con llaves RSA nuevas: nunca se
  usan ni se guardan certificados o CAF reales.
- Las pruebas no llaman al SII. ``FakeSii`` reemplaza los métodos del cliente
  SII; la firma calcula los montos con la regla del SII a partir del detalle
  enviado, para verificar de verdad que coinciden con los de Odoo.
"""
from __future__ import annotations

import base64
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase

from odoo.addons.tf_dte_cl.models import sii_client as sc
from odoo.addons.tf_dte_cl.models.res_partner import rut_check_digit

# PNG de 1x1: el timbre solo debe ser una imagen válida.
TINY_PNG = ('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==')


def make_rut(body: str) -> str:
    return '%s-%s' % (body, rut_check_digit(body))


COMPANY_RUT = make_rut('76086428')
CUSTOMER_RUT = make_rut('96000001')
SIGNER_RUT = make_rut('12345678')
P12_PASSWORD = 'clave-de-prueba'


def round_half_up(value) -> int:
    return int(Decimal(str(value)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def make_p12(rut: str = SIGNER_RUT, days: int = 365) -> bytes:
    """Certificado PKCS#12 autofirmado con el RUT del firmante en serialNumber."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, 'Firmante de prueba'),
        x509.NameAttribute(NameOID.SERIAL_NUMBER, rut),
        x509.NameAttribute(NameOID.EMAIL_ADDRESS, 'firmante@prueba.cl'),
    ])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=days))
            .sign(key, hashes.SHA256()))
    return pkcs12.serialize_key_and_certificates(
        b'prueba', key, cert, None, serialization.BestAvailableEncryption(P12_PASSWORD.encode()),
    )


def _b64_int(value: int) -> str:
    return base64.b64encode(value.to_bytes((value.bit_length() + 7) // 8, 'big')).decode()


def make_caf(rut: str, document_type: str, start: int, end: int, idk: str = '100',
             authorized: date | None = None, with_signature: bool = True, key=None) -> bytes:
    """CAF con la estructura del SII y una llave RSA generada para la prueba."""
    key = key or rsa.generate_private_key(public_exponent=65537, key_size=1024)
    numbers = key.public_key().public_numbers()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    authorized = authorized or (date.today() - timedelta(days=5))
    signature = '<FRMA algoritmo="SHA1withRSA">cHJ1ZWJh</FRMA>' if with_signature else ''
    xml = (
        '<?xml version="1.0"?>\n'
        '<AUTORIZACION><CAF version="1.0"><DA>'
        '<RE>%(rut)s</RE><RS>EMPRESA DE PRUEBA SPA</RS><TD>%(td)s</TD>'
        '<RNG><D>%(start)s</D><H>%(end)s</H></RNG><FA>%(fa)s</FA>'
        '<RSAPK><M>%(m)s</M><E>%(e)s</E></RSAPK><IDK>%(idk)s</IDK>'
        '</DA>%(frma)s</CAF>'
        '<RSASK>%(sk)s</RSASK><RSAPUBK>%(pk)s</RSAPUBK></AUTORIZACION>'
    ) % {
        'rut': rut, 'td': document_type, 'start': start, 'end': end, 'fa': authorized.isoformat(),
        'm': _b64_int(numbers.n), 'e': _b64_int(numbers.e), 'idk': idk, 'frma': signature,
        'sk': private_pem, 'pk': public_pem,
    }
    return xml.encode('ISO-8859-1')


def sii_amounts(document: dict) -> dict:
    """Totales con la regla del SII a partir del detalle enviado a la librería."""
    rate = document.get('TasaIVA') or 19
    net = exempt = 0
    additional = defaultdict(int)
    for item in document.get('Detalle') or []:
        amount = item['MontoItem']
        if item.get('IndExe'):
            exempt += amount
            continue
        taxes = [(tax['CodImp'], tax.get('TasaImp')) for tax in item.get('Impuesto') or []]
        if any(code == 14 for code, _rate in taxes):
            net += amount
        for code, tax_rate in taxes:
            if code != 14:
                additional[(code, tax_rate)] += amount
    iva = round_half_up(Decimal(net) * Decimal(str(rate)) / 100)
    extra = sum(round_half_up(Decimal(base) * Decimal(str(r)) / 100) for (_c, r), base in additional.items())
    return {'MntNeto': net, 'MntExe': exempt, 'MntIVA': iva, 'ImptoReten': extra,
            'MntTotal': net + exempt + iva + extra}


class FakeSii:
    """Reemplazo del cliente SII. Cada atributo controla una respuesta."""

    def __init__(self):
        self.amount_delta = 0            # suma algo al total para simular una diferencia
        self.sign_ok = True
        self.send_outcomes = []          # cola de SendResult; si está vacía, envío OK
        self.upload_state = sc.UPLOAD_STATE_ACCEPTED
        self.document_code = 'DOK'
        self.calls = defaultdict(int)
        self.last_sign_payload = None

    def sign(self, client, payload):
        self.calls['sign'] += 1
        self.last_sign_payload = payload
        group = payload['Documento'][0]
        document = group['documentos'][0]
        if not self.sign_ok:
            return [sc.SignResult(ok=False, message='Error simulado de la librería')]
        amounts = sii_amounts(document)
        amounts['MntTotal'] += self.amount_delta
        return [sc.SignResult(
            ok=True, tipo_dte=group['TipoDTE'], xml='<DTE version="1.0"/>', ted='<TED version="1.0"/>',
            barcode_png_b64=TINY_PNG, amounts=amounts,
        )]

    def build_envelope(self, client, payload):
        self.calls['envelope'] += 1
        return sc.EnvelopeResult(
            ok=True, xml='<?xml version="1.0" encoding="ISO-8859-1"?><EnvioDTE/>',
            filename=payload.get('filename'),
        )

    def send_envelope(self, client, payload):
        self.calls['send'] += 1
        if self.send_outcomes:
            return self.send_outcomes.pop(0)
        return sc.SendResult(outcome=sc.SEND_OK, track_id='1000%s' % self.calls['send'])

    def query_upload(self, client, payload):
        self.calls['query_upload'] += 1
        return sc.QueryResult(ok=True, code='EPR', state=self.upload_state, detail='Envío procesado')

    def query_document(self, client, payload):
        self.calls['query_document'] += 1
        code = self.document_code
        return sc.QueryResult(ok=True, code=code, received=sc.document_was_received(code), detail=code)


class TfDteClCommon(TransactionCase):
    """Compañía emisora completa, cliente, impuesto, diarios y CAF de certificación."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.chile = env.ref('base.cl')
        clp = env.ref('base.CLP')
        clp.active = True
        cls.comuna = env['tf_dte_cl.comuna'].search([('country_id', '=', cls.chile.id)], limit=1)
        cls.activity = env['tf_dte_cl.activity'].search([], limit=1)
        assert cls.comuna and cls.activity, 'Faltan los catálogos de comunas o actividades del módulo.'

        cls.company = env.company
        # La base de pruebas se crea sin país (para no instalar l10n_cl), así que no tiene
        # plan de cuentas: se carga el genérico de Odoo antes de configurar la compañía.
        if not cls.company.chart_template:
            env['account.chart.template'].try_loading('generic_coa', cls.company, install_demo=False)
        cls.company.write({
            'name': 'Empresa de Prueba SpA',
            'vat': COMPANY_RUT,
            'street': 'Calle de Prueba 123',
            'city': 'Santiago',
            'country_id': cls.chile.id,
            'account_fiscal_country_id': cls.chile.id,
            'email': 'dte@prueba.cl',
            'currency_id': clp.id,
            'tax_calculation_rounding_method': 'round_globally',
            'tf_dte_cl_giro': 'Servicios informáticos',
            'tf_dte_cl_comuna_id': cls.comuna.id,
            'tf_dte_cl_activity_ids': [Command.set(cls.activity.ids)],
            'tf_dte_cl_resolution_date': date(2014, 8, 22),
            'tf_dte_cl_resolution_number': 0,
            'tf_dte_cl_environment': 'certificacion',
            'tf_dte_cl_sii_office': 'SANTIAGO CENTRO',
        })
        cls.env.user.groups_id |= env.ref('tf_dte_cl.group_tf_dte_cl_manager')

        wizard = env['tf_dte_cl.certificate.load'].create({
            'company_id': cls.company.id,
            'p12_file': base64.b64encode(make_p12()),
            'p12_filename': 'prueba.p12',
            'password': P12_PASSWORD,
            'make_default': True,
        })
        wizard.action_load()
        cls.certificate = cls.company.tf_dte_cl_certificate_id

        cls.tax_group_iva = env['account.tax.group'].create({
            'name': 'IVA 19% (prueba)',
            'company_id': cls.company.id,
            'country_id': cls.chile.id,
        })
        cls.tax_iva = env['account.tax'].create({
            'name': 'IVA 19% venta (prueba)',
            'tax_group_id': cls.tax_group_iva.id,
            'amount': 19.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'tf_dte_cl_sii_code': '14',
            'company_id': cls.company.id,
            'country_id': cls.chile.id,
        })
        Account = env['account.account']
        cls.income_account = Account.search([
            *Account._check_company_domain(cls.company), ('account_type', '=', 'income'),
        ], limit=1)
        assert cls.income_account, 'La compañía de prueba no tiene cuentas de ingresos.'
        cls.journal_33 = cls._create_sale_journal('TF33', '33')
        cls.journal_61 = cls._create_sale_journal('TF61', '61')

        cls.partner = env['res.partner'].create({
            'name': 'Cliente de Prueba SpA',
            'vat': CUSTOMER_RUT,
            'is_company': True,
            'street': 'Av. Siempre Viva 742',
            'city': 'Santiago',
            'country_id': cls.chile.id,
            'tf_dte_cl_giro': 'Comercio',
            'tf_dte_cl_comuna_id': cls.comuna.id,
            'tf_dte_cl_dte_email': 'dte@cliente.cl',
        })
        cls.product = env['product.product'].create({
            'name': 'Servicio de prueba',
            'default_code': 'SRV1',
            'type': 'service',
            'list_price': 10000,
            'taxes_id': [Command.set(cls.tax_iva.ids)],
        })
        cls.caf_33 = cls.create_caf('33', 1, 50)
        cls.caf_61 = cls.create_caf('61', 1, 50)

    @classmethod
    def _create_sale_journal(cls, code: str, document_type: str):
        return cls.env['account.journal'].create({
            'name': 'Diario DTE %s' % document_type,
            'code': code,
            'type': 'sale',
            'company_id': cls.company.id,
            'default_account_id': cls.income_account.id,
            'tf_dte_cl_document_type': document_type,
        })

    @classmethod
    def create_caf(cls, document_type: str, start: int, end: int, **kwargs):
        return cls.env['tf_dte_cl.caf'].create({
            'company_id': cls.company.id,
            'caf_file': base64.b64encode(make_caf(COMPANY_RUT, document_type, start, end, **kwargs)),
            'caf_filename': 'caf_%s_%s_%s.xml' % (document_type, start, end),
        })

    def create_invoice(self, journal=None, partner=None, move_type='out_invoice', quantity=2, price=10000,
                       taxes=None, **values):
        taxes = self.tax_iva if taxes is None else taxes
        return self.env['account.move'].create({
            'move_type': move_type,
            'journal_id': (journal or self.journal_33).id,
            'partner_id': (partner or self.partner).id,
            'invoice_date': fields.Date.context_today(self.env.user),
            'invoice_line_ids': [Command.create({
                'product_id': self.product.id,
                'quantity': quantity,
                'price_unit': price,
                'tax_ids': [Command.set(taxes.ids)],
            })],
            **values,
        })

    @contextmanager
    def assertUserError(self, fragment: str):
        """Como assertRaises(UserError) de Odoo (con savepoint, deshace el bloque) y verifica el mensaje."""
        with self.assertRaises(UserError) as caught:
            yield caught
        self.env.invalidate_all()
        self.assertIn(fragment, str(caught.exception))

    @contextmanager
    def fake_sii(self):
        """Reemplaza el cliente SII por ``FakeSii`` durante el bloque."""
        fake = FakeSii()
        client_class = type(self.env['tf_dte_cl.sii.client'])
        with patch.object(client_class, 'tf_dte_cl_sign', lambda c, p: fake.sign(c, p)), \
                patch.object(client_class, 'tf_dte_cl_build_envelope', lambda c, p: fake.build_envelope(c, p)), \
                patch.object(client_class, 'tf_dte_cl_send_envelope', lambda c, p: fake.send_envelope(c, p)), \
                patch.object(client_class, 'tf_dte_cl_query_upload', lambda c, p: fake.query_upload(c, p)), \
                patch.object(client_class, 'tf_dte_cl_query_document', lambda c, p: fake.query_document(c, p)):
            yield fake
