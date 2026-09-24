# -*- coding: utf-8 -*-
"""Códigos de Autorización de Folios (CAF) y asignación de folios.

Ruta real: models/caf.py

Reglas:
* El archivo se guarda con sus bytes originales (el nodo CAF viaja dentro del
  TED y debe coincidir con lo que firmó el SII).
* La librería solo valida el rango; aquí se validan tipo, RUT, rango, llaves,
  solapamiento, codificación y vigencia.
* El folio sale de ``next_folio`` bajo bloqueo de fila. Como es una columna
  transaccional, un rollback lo devuelve: ``_tf_dte_cl_take_folio`` debe
  llamarse dentro del mismo savepoint que la firma.
* Un folio firmado que nunca llegó al SII no se reutiliza: se registra en
  ``tf_dte_cl.caf.void`` para anularlo en el sitio del SII.
"""
from __future__ import annotations

import base64
import re
from datetime import date

from dateutil.relativedelta import relativedelta
from lxml import etree

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from .res_partner import normalize_rut

try:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
except ImportError:  # pragma: no cover - cryptography es dependencia de Odoo 18
    load_pem_private_key = None

DTE_TYPES = [
    ('33', 'Factura electrónica (33)'),
    ('34', 'Factura no afecta o exenta electrónica (34)'),
    ('52', 'Guía de despacho electrónica (52)'),
    ('56', 'Nota de débito electrónica (56)'),
    ('61', 'Nota de crédito electrónica (61)'),
]
DTE_TYPE_CODES = frozenset(code for code, _label in DTE_TYPES)

# IDK del CAF: identifica la llave con que el SII firmó el CAF y, con ello, el
# ambiente en que es válido. Valores observados en CAF reales emitidos por el SII.
IDK_ENVIRONMENTS = {'100': 'certificacion', '300': 'produccion'}
CAF_ENVIRONMENTS = [('certificacion', 'Certificación'), ('produccion', 'Producción')]


def environment_from_idk(idk) -> str | bool:
    """Ambiente del CAF según su IDK, o False si el valor no es conocido."""
    return IDK_ENVIRONMENTS.get(str(idk or '').strip(), False)

_XML_DECLARATION_RE = re.compile(rb'^\s*<\?xml[^>]*\?>\s*')
_DECLARED_ENCODING_RE = re.compile(rb'^\s*<\?xml[^>]*encoding=["\']([A-Za-z0-9._-]+)["\']')


class CafError(ValueError):
    """Archivo CAF inválido (mensaje apto para el usuario)."""


def _text(root, path: str) -> str:
    node = root.find(path)
    if node is None or not (node.text or '').strip():
        raise CafError('El archivo CAF no contiene el nodo %s.' % path)
    return node.text.strip()


def _b64_int(text: str) -> int:
    return int.from_bytes(base64.b64decode(text), 'big')


def parse_caf(raw: bytes) -> dict:
    """Valida la estructura de un CAF y devuelve sus datos.

    No depende de Odoo; lanza ``CafError`` con un mensaje para el usuario.
    """
    if not raw:
        raise CafError('El archivo CAF está vacío.')
    parser = etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False)
    # Sin declaración de codificación, se lee como ISO-8859-1, igual que la librería.
    declared = _DECLARED_ENCODING_RE.match(raw)
    file_parser = parser if declared else etree.XMLParser(
        resolve_entities=False, no_network=True, remove_blank_text=False, encoding='ISO-8859-1',
    )
    try:
        root = etree.fromstring(raw, file_parser)
    except etree.XMLSyntaxError as error:
        raise CafError('El archivo CAF no es un XML válido: %s' % error) from None
    if root.tag != 'AUTORIZACION':
        raise CafError('El archivo no es un CAF del SII (se esperaba el nodo AUTORIZACION).')

    document_type = _text(root, 'CAF/DA/TD')
    folio_from = int(_text(root, 'CAF/DA/RNG/D'))
    folio_to = int(_text(root, 'CAF/DA/RNG/H'))
    try:
        authorized = date.fromisoformat(_text(root, 'CAF/DA/FA'))
    except ValueError:
        raise CafError('La fecha de autorización (FA) del CAF no es válida.') from None
    if folio_from < 1 or folio_from > folio_to:
        raise CafError('El rango de folios del CAF no es válido (%s a %s).' % (folio_from, folio_to))
    if root.find('CAF/FRMA') is None:
        raise CafError('El CAF no trae la firma del SII (FRMA).')

    # La librería decodifica el CAF como ISO-8859-1 y lo parsea como texto:
    # debe producir el mismo contenido que el archivo original.
    body = _XML_DECLARATION_RE.sub(b'', raw)
    try:
        as_library = etree.fromstring(body.decode('ISO-8859-1'), parser)
    except (etree.XMLSyntaxError, ValueError):
        raise CafError('El CAF tiene una codificación que la librería no puede leer.') from None
    if as_library.findtext('CAF/DA/RS') != root.findtext('CAF/DA/RS'):
        raise CafError('La codificación del CAF no es ISO-8859-1; vuelva a descargarlo desde el SII.')

    private_pem = _text(root, 'RSASK').replace('\t', '')
    if load_pem_private_key is None:
        raise CafError('Falta la librería "cryptography" en el servidor.')
    try:
        private_key = load_pem_private_key(private_pem.encode('ascii'), password=None)
    except (ValueError, TypeError):
        raise CafError('La llave privada del CAF (RSASK) no es válida.') from None
    modulus, exponent = root.findtext('CAF/DA/RSAPK/M'), root.findtext('CAF/DA/RSAPK/E')
    if modulus and exponent:
        numbers = private_key.public_key().public_numbers()
        if (numbers.n, numbers.e) != (_b64_int(modulus), _b64_int(exponent)):
            raise CafError('La llave privada del CAF no corresponde a su llave pública (RSAPK).')

    return {
        'document_type': document_type,
        'rut': normalize_rut(_text(root, 'CAF/DA/RE')),
        'company_name': root.findtext('CAF/DA/RS') or '',
        'folio_from': folio_from,
        'folio_to': folio_to,
        'date_authorized': authorized,
        'idk': root.findtext('CAF/DA/IDK') or '',
        'library_payload': base64.b64encode(body),
    }


class TfDteClCaf(models.Model):
    _name = 'tf_dte_cl.caf'
    _description = 'CAF (Código de Autorización de Folios)'
    _inherit = ['mail.thread']
    _order = 'company_id, document_type, folio_from'
    _check_company_auto = True

    name = fields.Char(string='Nombre', compute='_compute_name', store=True)
    company_id = fields.Many2one(
        'res.company', string='Compañía', required=True, index=True,
        default=lambda self: self.env.company,
    )
    active = fields.Boolean(default=True, tracking=True)
    caf_file = fields.Binary(string='Archivo CAF', required=True, attachment=True, copy=False)
    caf_filename = fields.Char(string='Nombre del archivo')
    document_type = fields.Selection(DTE_TYPES, string='Tipo de documento', readonly=True, index=True)
    rut = fields.Char(string='RUT autorizado', readonly=True)
    folio_from = fields.Integer(string='Folio desde', readonly=True)
    folio_to = fields.Integer(string='Folio hasta', readonly=True)
    next_folio = fields.Integer(
        string='Próximo folio', tracking=True, copy=False,
        help='Solo puede aumentarse manualmente, por ejemplo si parte de los folios ya se usó en otro sistema.',
    )
    date_authorized = fields.Date(string='Fecha de autorización', readonly=True)
    date_expiry = fields.Date(
        string='Usar hasta', compute='_compute_date_expiry', store=True,
        help='Fecha de autorización más la vigencia configurada en la compañía.',
    )
    idk = fields.Char(string='IDK', readonly=True)
    environment = fields.Selection(
        CAF_ENVIRONMENTS, string='Ambiente', compute='_compute_environment', store=True, index=True,
        help='Ambiente del SII en que es válido el CAF, según su IDK. Los folios solo se toman de CAF '
             'del ambiente configurado en la compañía.',
    )
    folios_available = fields.Integer(string='Folios disponibles', compute='_compute_folios', store=True)
    exhausted = fields.Boolean(string='Agotado', compute='_compute_folios', store=True)
    state = fields.Selection(
        [('available', 'Disponible'), ('exhausted', 'Agotado'), ('expired', 'Vencido'), ('archived', 'Archivado')],
        string='Estado', compute='_compute_state',
    )
    void_ids = fields.One2many('tf_dte_cl.caf.void', 'caf_id', string='Folios por anular')

    _sql_constraints = [
        ('company_type_from_uniq', 'unique(company_id, document_type, folio_from)',
         'Este CAF ya está cargado en la compañía.'),
    ]

    # ------------------------------------------------------------------
    # Cómputos
    # ------------------------------------------------------------------
    @api.depends('document_type', 'folio_from', 'folio_to')
    def _compute_name(self):
        for caf in self:
            caf.name = 'CAF %s [%s-%s]' % (caf.document_type or '?', caf.folio_from, caf.folio_to)

    @api.depends('date_authorized', 'company_id.tf_dte_cl_caf_validity_months')
    def _compute_date_expiry(self):
        for caf in self:
            months = caf.company_id.tf_dte_cl_caf_validity_months or 0
            caf.date_expiry = caf.date_authorized and caf.date_authorized + relativedelta(months=months)

    @api.depends('idk')
    def _compute_environment(self):
        for caf in self:
            caf.environment = environment_from_idk(caf.idk)

    @api.depends('next_folio', 'folio_to')
    def _compute_folios(self):
        for caf in self:
            caf.folios_available = max(caf.folio_to - caf.next_folio + 1, 0) if caf.folio_to else 0
            caf.exhausted = bool(caf.folio_to) and caf.next_folio > caf.folio_to

    @api.depends('active', 'exhausted', 'date_expiry')
    def _compute_state(self):
        today = fields.Date.context_today(self)
        for caf in self:
            if not caf.active:
                caf.state = 'archived'
            elif caf.exhausted:
                caf.state = 'exhausted'
            elif caf.date_expiry and caf.date_expiry < today:
                caf.state = 'expired'
            else:
                caf.state = 'available'

    # ------------------------------------------------------------------
    # Carga y restricciones
    # ------------------------------------------------------------------
    def _tf_dte_cl_values_from_file(self, caf_file, company) -> dict:
        try:
            data = parse_caf(base64.b64decode(caf_file or b''))
        except CafError as error:
            raise UserError(str(error)) from None
        if data['document_type'] not in DTE_TYPE_CODES:
            raise UserError(self.env._(
                'El CAF es para el tipo de documento %s, que no está dentro del alcance de este módulo.',
                data['document_type'],
            ))
        if data['rut'] != normalize_rut(company.vat):
            raise UserError(self.env._(
                'El CAF fue emitido para el RUT %(caf)s y la compañía tiene el RUT %(company)s.',
                caf=data['rut'], company=company.vat or '-',
            ))
        return {
            'document_type': data['document_type'],
            'rut': data['rut'],
            'folio_from': data['folio_from'],
            'folio_to': data['folio_to'],
            'date_authorized': data['date_authorized'],
            'idk': data['idk'],
        }

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            company = self.env['res.company'].browse(vals.get('company_id')) or self.env.company
            vals.update(self._tf_dte_cl_values_from_file(vals.get('caf_file'), company))
            # El cliente web envía next_folio = 0 por defecto: se parte desde el primer folio.
            if not vals.get('next_folio') or vals['next_folio'] < vals['folio_from']:
                vals['next_folio'] = vals['folio_from']
        cafs = super().create(vals_list)
        for caf in cafs:
            caf._tf_dte_cl_warn_environment()
        return cafs

    def _tf_dte_cl_warn_environment(self) -> None:
        """Avisa si el CAF no corresponde al ambiente actual de la compañía (no lo impide)."""
        self.ensure_one()
        company_environment = self.company_id.tf_dte_cl_environment
        if not self.environment:
            self.message_post(body=self.env._(
                'No se reconoce el ambiente de este CAF (IDK %s): se usará en cualquier ambiente.',
                self.idk or '-',
            ))
        elif self.environment != company_environment:
            self.message_post(body=self.env._(
                'Este CAF es de %(caf_env)s y la compañía está en %(company_env)s: sus folios no se '
                'usarán hasta que la compañía cambie de ambiente.',
                caf_env=dict(CAF_ENVIRONMENTS)[self.environment],
                company_env=dict(CAF_ENVIRONMENTS).get(company_environment, company_environment),
            ))

    def write(self, vals):
        if 'caf_file' in vals:
            raise UserError(self.env._('No se puede reemplazar el archivo de un CAF; cargue uno nuevo.'))
        if 'company_id' in vals and any(caf.company_id.id != vals['company_id'] for caf in self):
            raise UserError(self.env._('No se puede cambiar la compañía de un CAF.'))
        if 'next_folio' in vals and any(vals['next_folio'] < caf.next_folio for caf in self):
            raise UserError(self.env._('El próximo folio no puede retroceder.'))
        return super().write(vals)

    def unlink(self):
        if any(caf.next_folio > caf.folio_from for caf in self):
            raise UserError(self.env._('No se puede eliminar un CAF con folios usados; archívelo.'))
        return super().unlink()

    @api.constrains('next_folio', 'folio_from', 'folio_to')
    def _check_next_folio(self):
        for caf in self:
            if not caf.folio_from <= caf.next_folio <= caf.folio_to + 1:
                raise ValidationError(self.env._(
                    'El próximo folio de %(caf)s debe estar entre %(start)s y %(end)s.',
                    caf=caf.name, start=caf.folio_from, end=caf.folio_to + 1,
                ))

    @api.constrains('company_id', 'document_type', 'folio_from', 'folio_to', 'environment')
    def _check_overlap(self):
        for caf in self:
            # Los rangos de certificación y de producción son independientes: pueden coincidir.
            overlapping = self.with_context(active_test=False).search([
                ('id', '!=', caf.id),
                ('company_id', '=', caf.company_id.id),
                ('document_type', '=', caf.document_type),
                ('environment', '=', caf.environment),
                ('folio_from', '<=', caf.folio_to),
                ('folio_to', '>=', caf.folio_from),
            ], limit=1)
            if overlapping:
                raise ValidationError(self.env._(
                    'El rango de %(caf)s se solapa con %(other)s.', caf=caf.name, other=overlapping.name,
                ))

    # ------------------------------------------------------------------
    # Uso desde la emisión
    # ------------------------------------------------------------------
    @api.model
    def _tf_dte_cl_unavailability_reason(self, company, document_type: str) -> str | bool:
        """Motivo por el que no hay folios, o ``False`` si hay al menos uno disponible."""
        all_cafs = self.with_context(active_test=True).search([
            ('company_id', '=', company.id), ('document_type', '=', document_type),
        ])
        environment = company.tf_dte_cl_environment
        cafs = all_cafs.filtered(lambda c: c.environment in (environment, False))
        if not cafs:
            if all_cafs:
                return self.env._(
                    'Solo hay CAF de otro ambiente para el tipo de documento %(type)s; la compañía '
                    'está en %(env)s. Cargue un CAF de %(env)s.',
                    type=document_type, env=dict(CAF_ENVIRONMENTS).get(environment, environment),
                )
            return self.env._('No hay un CAF cargado para el tipo de documento %s.', document_type)
        today = fields.Date.context_today(self)
        usable = cafs.filtered(lambda c: not c.exhausted and c.date_expiry and c.date_expiry >= today)
        if usable:
            return False
        if cafs.filtered(lambda c: not c.exhausted):
            return self.env._(
                'El CAF %s está vencido; solicite uno nuevo en el SII y anule los folios no usados.',
                document_type,
            )
        return self.env._('CAF %s agotado; solicite y cargue uno nuevo.', document_type)

    @api.model
    def _tf_dte_cl_take_folio(self, company, document_type: str) -> tuple[models.Model, int]:
        """Reserva el siguiente folio bajo bloqueo de fila.

        Debe ejecutarse dentro del mismo savepoint que la firma: si esta falla,
        el rollback devuelve el folio al CAF.
        """
        today = fields.Date.context_today(self)
        self.flush_model(['next_folio', 'exhausted', 'date_expiry', 'active', 'environment'])
        self.env.cr.execute(
            """
            SELECT id
              FROM tf_dte_cl_caf
             WHERE company_id = %s
               AND document_type = %s
               AND (environment = %s OR environment IS NULL)
               AND active
               AND NOT exhausted
               AND date_expiry >= %s
             ORDER BY environment IS NULL, folio_from
             LIMIT 1
               FOR UPDATE
            """,
            [company.id, document_type, company.tf_dte_cl_environment, today],
        )
        row = self.env.cr.fetchone()
        if not row:
            raise UserError(
                self._tf_dte_cl_unavailability_reason(company, document_type)
                or self.env._('No hay folios disponibles para el tipo de documento %s.', document_type)
            )
        caf = self.browse(row[0])
        caf.invalidate_recordset(['next_folio'])
        folio = caf.next_folio
        caf.sudo().write({'next_folio': folio + 1})
        return caf, folio

    @api.model
    def _tf_dte_cl_find_for_folio(self, company, document_type: str, folio: int):
        # Los rangos de ambos ambientes pueden coincidir: se busca en el ambiente actual.
        caf = self.with_context(active_test=False).search([
            ('company_id', '=', company.id),
            ('document_type', '=', document_type),
            ('environment', 'in', [company.tf_dte_cl_environment, False]),
            ('folio_from', '<=', folio),
            ('folio_to', '>=', folio),
        ], order='environment', limit=1)
        if not caf:
            raise UserError(self.env._(
                'No hay un CAF cargado que contenga el folio %(folio)s del tipo %(type)s.',
                folio=folio, type=document_type,
            ))
        return caf

    def _tf_dte_cl_library_payload(self) -> list[bytes]:
        """Valor de ``caf_file`` para facturacion_electronica: solo este CAF."""
        self.ensure_one()
        return [parse_caf(base64.b64decode(self.caf_file))['library_payload']]


class TfDteClCafVoid(models.Model):
    _name = 'tf_dte_cl.caf.void'
    _description = 'Folio por anular en el SII'
    _order = 'state, document_type, folio'
    _check_company_auto = True

    caf_id = fields.Many2one('tf_dte_cl.caf', string='CAF', required=True, ondelete='restrict', index=True)
    company_id = fields.Many2one(related='caf_id.company_id', store=True, index=True)
    document_type = fields.Selection(related='caf_id.document_type', store=True)
    folio = fields.Integer(string='Folio', required=True)
    reason = fields.Char(string='Motivo', required=True)
    res_model = fields.Char(string='Modelo de origen', readonly=True)
    res_id = fields.Many2oneReference(string='Documento de origen', model_field='res_model', readonly=True)
    state = fields.Selection(
        [('pending', 'Pendiente'), ('done', 'Anulado en el SII')],
        string='Estado', default='pending', required=True,
    )
    date_done = fields.Date(string='Fecha de anulación')

    _sql_constraints = [
        ('caf_folio_uniq', 'unique(caf_id, folio)', 'El folio ya está registrado para anulación.'),
    ]

    @api.constrains('caf_id', 'folio')
    def _check_folio_in_range(self):
        for void in self:
            if not void.caf_id.folio_from <= void.folio <= void.caf_id.folio_to:
                raise ValidationError(self.env._(
                    'El folio %(folio)s no pertenece a %(caf)s.', folio=void.folio, caf=void.caf_id.name,
                ))

    def action_mark_done(self):
        self.write({'state': 'done', 'date_done': fields.Date.context_today(self)})

    @api.model
    def _tf_dte_cl_register(self, company, document_type: str, folio: int, reason: str, record=None, caf=None):
        """Registra un folio firmado que no llegará al SII (idempotente).

        Si se conoce el CAF con que se firmó, se usa ese: los rangos de
        certificación y producción pueden coincidir.
        """
        caf = caf or self.env['tf_dte_cl.caf']._tf_dte_cl_find_for_folio(company, document_type, folio)
        existing = self.sudo().search([('caf_id', '=', caf.id), ('folio', '=', folio)], limit=1)
        if existing:
            return existing
        return self.sudo().create({
            'caf_id': caf.id,
            'folio': folio,
            'reason': reason,
            'res_model': record._name if record else False,
            'res_id': record.id if record else False,
        })
