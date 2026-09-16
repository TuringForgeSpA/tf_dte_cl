# -*- coding: utf-8 -*-
"""Tipos de documento que se pueden citar en una referencia (TpoDocRef).

Ruta real: models/document_type.py

Fuente: SII, "Formato Documentos Tributarios Electrónicos" v2.4.2 (2024-02),
sección E "Información de referencia", campo 2. Los registros oficiales se
cargan desde data/tf_dte_cl.document_type.csv.

Según el manual, un TpoDocRef alfabético no se valida en el SII y el
contribuyente puede usarlo para documentos no tributarios propios; por eso se
permiten tipos "libres" con código no numérico.
"""
from odoo import api, fields, models
from odoo.exceptions import ValidationError

KIND_TAX = 'tax'      # documento tributario: folio numérico
KIND_OTHER = 'other'  # serie 800 (no tributario): folio alfanumérico
KIND_FREE = 'free'    # código alfabético del contribuyente: sin validación SII


class TfDteClDocumentType(models.Model):
    _name = 'tf_dte_cl.document_type'
    _description = 'Tipo de documento de referencia SII'
    _order = 'kind, code'
    _rec_names_search = ['code', 'name']

    code = fields.Char(string='Código', required=True, index=True)
    name = fields.Char(string='Nombre', required=True)
    kind = fields.Selection(
        [(KIND_TAX, 'Tributario'), (KIND_OTHER, 'No tributario (serie 800)'), (KIND_FREE, 'Libre')],
        string='Clase', required=True, default=KIND_FREE,
    )
    is_electronic = fields.Boolean(string='Electrónico')
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('code_uniq', 'unique(code)', 'Ya existe un tipo de documento con ese código.'),
    ]

    @api.depends('code', 'name')
    def _compute_display_name(self):
        for doc_type in self:
            doc_type.display_name = '%s - %s' % (doc_type.code, doc_type.name) if doc_type.code else doc_type.name

    @api.constrains('code', 'kind')
    def _check_code(self):
        for doc_type in self:
            code = doc_type.code or ''
            if len(code) > 3:
                raise ValidationError(self.env._('El código de tipo de documento admite hasta 3 caracteres.'))
            if doc_type.kind == KIND_FREE and code.isdigit():
                raise ValidationError(self.env._(
                    'Los códigos numéricos están reservados para los tipos oficiales del SII; '
                    'use un código alfabético para documentos propios.'
                ))
            if doc_type.kind != KIND_FREE and not code.isdigit():
                raise ValidationError(self.env._('Los tipos oficiales del SII tienen código numérico.'))
