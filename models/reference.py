# -*- coding: utf-8 -*-
"""Líneas de referencia de un DTE (zona E del documento).

Ruta real: models/reference.py

Fuente: SII, "Formato Documentos Tributarios Electrónicos" v2.4.2 (2024-02),
sección E "Información de referencia".

Donde el manual no es explícito, la decisión tomada está marcada como
"Criterio tf_dte_cl".

Límites de facturacion_electronica 0.24.0 (documento_referencias.py):
* La clase ``Referencia`` no define ``IndGlobal`` ni ``RUTOtr``: ``set_from_keys``
  los descarta con un warning y nunca llegan al XML. Enviar una referencia global
  sin ``IndGlobal`` dejaría un ``FolioRef`` 0 inválido, por lo que ambas opciones
  se bloquean hasta cubrir ese hueco.
* Con ``CodRef`` 2, la librería exige que ``RazonRef`` contenga "DICE:" seguido de
  "DEBE DECIR:" (en una sola línea) y falla si la razón está vacía.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from datetime import date

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .document_type import KIND_OTHER, KIND_TAX
from .res_partner import is_valid_rut, normalize_rut

MAX_REFERENCES = 40
MAX_FOLIO_LENGTH = 18       # FolioRType
MAX_TAX_FOLIO_DIGITS = 10   # FolioType
MAX_REASON_LENGTH = 90
DATE_MIN = date(2002, 8, 1)
DATE_MAX = date(2050, 12, 31)

CODE_CANCEL = '1'           # Anula documento de referencia
CODE_FIX_TEXT = '2'         # Corrige texto del documento de referencia
CODE_FIX_AMOUNTS = '3'      # Corrige montos
REFERENCE_CODES = [
    (CODE_CANCEL, 'Anula documento de referencia'),
    (CODE_FIX_TEXT, 'Corrige texto del documento de referencia'),
    (CODE_FIX_AMOUNTS, 'Corrige montos'),
]

# Campos del manual que facturacion_electronica 0.24.0 no transmite.
LIBRARY_SUPPORTS_GLOBAL = False
LIBRARY_SUPPORTS_OTHER_RUT = False
# Misma expresión que documento.py (Referencia, CodRef == 2), aplicada sobre upper().
FIX_TEXT_REASON_RE = re.compile(r'DICE:(.*)DEBE DECIR:')

NOTE_TYPES = ('56', '61')
CREDIT_NOTE_TYPES = ('60', '61')
# Caso a): la NC que anula elimina una factura de venta, una ND o una factura de compra.
CANCELLABLE_BY_CREDIT_NOTE = ('30', '32', '33', '34', '45', '46', '55', '56')


def validate_references(dte_type: str, lines: list[dict]) -> list[str]:
    """Valida las referencias de un DTE. No depende de Odoo.

    Cada línea es un dict con: ``type_code``, ``type_kind``, ``folio``,
    ``is_global``, ``date``, ``other_rut``, ``code``, ``reason``.
    Devuelve la lista de errores (vacía si todo está bien).
    """
    errors = []
    if len(lines) > MAX_REFERENCES:
        errors.append('Un DTE admite hasta %s referencias.' % MAX_REFERENCES)
    if dte_type in NOTE_TYPES and not lines:
        errors.append('Las notas de crédito y débito deben referenciar el documento que modifican.')

    for index, line in enumerate(lines, start=1):
        prefix = 'Referencia %s: ' % index
        type_code, kind = line.get('type_code'), line.get('type_kind')
        folio = str(line.get('folio') or '').strip()
        if not type_code:
            errors.append(prefix + 'falta el tipo de documento.')
            continue
        if line.get('is_global') and not LIBRARY_SUPPORTS_GLOBAL:
            errors.append(prefix + 'las referencias globales aún no están soportadas por la librería de emisión.')
        if line.get('other_rut') and not LIBRARY_SUPPORTS_OTHER_RUT:
            errors.append(prefix + 'el RUT de otro contribuyente aún no está soportado por la librería de emisión.')
        if line.get('is_global'):
            if folio not in ('', '0'):
                errors.append(prefix + 'una referencia global debe tener folio 0.')
        elif not folio:
            errors.append(prefix + 'falta el folio.')
        elif len(folio) > MAX_FOLIO_LENGTH:
            errors.append(prefix + 'el folio admite hasta %s caracteres.' % MAX_FOLIO_LENGTH)
        elif kind == KIND_TAX and (
            not folio.isdigit() or int(folio) == 0 or len(folio.lstrip('0')) > MAX_TAX_FOLIO_DIGITS
        ):
            errors.append(prefix + 'el folio de un documento tributario debe ser numérico y mayor que 0.')
        ref_date = line.get('date')
        if not ref_date:
            errors.append(prefix + 'falta la fecha del documento.')
        elif not DATE_MIN <= ref_date <= DATE_MAX:
            errors.append(prefix + 'la fecha debe estar entre %s y %s.' % (DATE_MIN, DATE_MAX))
        if line.get('other_rut'):
            if dte_type not in NOTE_TYPES or kind != KIND_TAX:
                errors.append(prefix + 'el RUT de otro contribuyente solo se informa en notas de '
                                       'crédito o débito que referencian un documento tributario.')
            elif not is_valid_rut(line['other_rut']):
                errors.append(prefix + 'el RUT de otro contribuyente es inválido.')
        if len(line.get('reason') or '') > MAX_REASON_LENGTH:
            errors.append(prefix + 'la razón admite hasta %s caracteres.' % MAX_REASON_LENGTH)

        code = line.get('code')
        if not code:
            continue
        if code in (CODE_CANCEL, CODE_FIX_TEXT) and line.get('is_global'):
            errors.append(prefix + 'una anulación o corrección de texto no puede ser una referencia global.')
        if dte_type == '61' and code == CODE_CANCEL and type_code not in CANCELLABLE_BY_CREDIT_NOTE:
            errors.append(prefix + 'una nota de crédito solo anula facturas o notas de débito.')
        if dte_type == '56' and code == CODE_CANCEL and type_code not in CREDIT_NOTE_TYPES:
            errors.append(prefix + 'una nota de débito solo anula notas de crédito.')
        if dte_type == '56' and code == CODE_FIX_TEXT:
            errors.append(prefix + 'la corrección de texto corresponde a una nota de crédito.')
        if dte_type in NOTE_TYPES and kind != KIND_TAX:
            errors.append(prefix + 'el código de referencia solo aplica a documentos tributarios.')
        if code == CODE_FIX_TEXT and not FIX_TEXT_REASON_RE.findall((line.get('reason') or '').upper()):
            errors.append(prefix + 'una corrección de texto debe indicar en la razón '
                                   '"DICE: <texto actual> DEBE DECIR: <texto correcto>", en una sola línea.')

    if dte_type in NOTE_TYPES and lines:
        coded = [line for line in lines if line.get('code')]
        # Criterio tf_dte_cl: el manual marca CodRef como obligatorio en notas; se exige
        # en al menos una referencia a un documento tributario (las de serie 800 no lo llevan).
        if not any(line.get('type_kind') == KIND_TAX for line in coded):
            errors.append('La nota debe indicar el código de referencia (anula, corrige texto o '
                          'corrige montos) del documento tributario que modifica.')
        # Manual, campo 8: los casos a), b) y c) deben tener un único documento de referencia.
        if any(line.get('code') in (CODE_CANCEL, CODE_FIX_TEXT) for line in coded) and len(lines) > 1:
            errors.append('Una nota que anula o corrige texto debe tener una única referencia.')
    return errors


def build_references_payload(dte_type: str, lines: list[dict]) -> list[OrderedDict]:
    """Bloque ``Referencia`` para facturacion_electronica, en el orden del schema."""
    payload = []
    for index, line in enumerate(lines, start=1):
        item = OrderedDict()
        item['NroLinRef'] = index
        item['TpoDocRef'] = line['type_code']
        if line.get('is_global') and LIBRARY_SUPPORTS_GLOBAL:
            item['IndGlobal'] = 1
        item['FolioRef'] = '0' if line.get('is_global') else str(line['folio']).strip()
        if line.get('other_rut') and dte_type in NOTE_TYPES and LIBRARY_SUPPORTS_OTHER_RUT:
            item['RUTOtr'] = normalize_rut(line['other_rut'])
        item['FchRef'] = line['date']
        if line.get('code'):
            item['CodRef'] = int(line['code'])
        if line.get('reason'):
            item['RazonRef'] = line['reason'][:MAX_REASON_LENGTH]
        payload.append(item)
    return payload


class TfDteClReference(models.Model):
    _name = 'tf_dte_cl.reference'
    _description = 'Referencia de DTE'
    _order = 'sequence, id'

    sequence = fields.Integer(default=10)
    move_id = fields.Many2one('account.move', string='Documento contable', ondelete='cascade', index=True)
    picking_id = fields.Many2one('stock.picking', string='Guía de despacho', ondelete='cascade', index=True)
    document_type_id = fields.Many2one(
        'tf_dte_cl.document_type', string='Tipo de documento', required=True, ondelete='restrict',
    )
    document_type_kind = fields.Selection(related='document_type_id.kind')
    folio = fields.Char(string='Folio')
    is_global = fields.Boolean(
        string='Referencia global',
        help='El documento afecta a más de 20 documentos del mismo tipo (por ejemplo, todas las guías del mes). '
             'El folio se informa como 0 y el motivo se explica en la razón.',
    )
    date = fields.Date(string='Fecha del documento', required=True)
    other_rut = fields.Char(
        string='RUT de otro contribuyente',
        help='Solo en notas de crédito o débito que referencian un documento emitido por otro '
             'contribuyente (por ejemplo, una empresa fusionada o absorbida).',
    )
    code = fields.Selection(REFERENCE_CODES, string='Código de referencia')
    reason = fields.Char(string='Razón', size=MAX_REASON_LENGTH)

    @api.constrains('move_id', 'picking_id')
    def _check_single_owner(self):
        for reference in self:
            if bool(reference.move_id) == bool(reference.picking_id):
                raise ValidationError(self.env._(
                    'Cada referencia debe pertenecer a un documento contable o a una guía, no a ambos.'
                ))

    @api.onchange('is_global')
    def _onchange_is_global(self):
        if self.is_global:
            self.folio = '0'

    def _tf_dte_cl_line_values(self) -> list[dict]:
        return [{
            'type_code': reference.document_type_id.code,
            'type_kind': reference.document_type_id.kind,
            'folio': reference.folio,
            'is_global': reference.is_global,
            'date': reference.date,
            'other_rut': reference.other_rut,
            'code': reference.code,
            'reason': reference.reason,
        } for reference in self]

    def _tf_dte_cl_errors(self, dte_type: str) -> list[str]:
        return validate_references(dte_type, self._tf_dte_cl_line_values())

    def _tf_dte_cl_payload(self, dte_type: str) -> list[OrderedDict]:
        return build_references_payload(dte_type, self._tf_dte_cl_line_values())
