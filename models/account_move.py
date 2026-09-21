# -*- coding: utf-8 -*-
"""Facturas (33/34), notas de débito (56) y notas de crédito (61).

Ruta real: models/account_move.py

La numeración de Odoo (``name``) no se toca: el folio SII vive en
``tf_dte_cl_folio``. El DTE se firma al publicar (sin red) y el framework
account_edi se encarga del envío.
"""
from __future__ import annotations

from collections import defaultdict

from odoo import api, fields, models
from odoo.exceptions import UserError

from .account_edi_format import DTE_CODE, SUCCESS_STATES
from .dte_lines import LineInfo, build_detail, iva_rate, line_errors, split_amounts, IVA_CODE
from .reference import CODE_CANCEL, CODE_FIX_AMOUNTS

SALE_MOVE_TYPES = ('out_invoice', 'out_refund')
INVOICE_DTE_TYPES = ('33', '34', '56')
CREDIT_NOTE_DTE_TYPE = '61'


class AccountMove(models.Model):
    # _name explícito: sin él, Odoo arma un modelo nuevo al combinar la extensión
    # con el mixin y duplica los campos heredados (p. ej. los Many2many).
    _name = 'account.move'
    _inherit = ['account.move', 'tf_dte_cl.document.mixin']

    tf_dte_cl_reference_ids = fields.One2many(
        'tf_dte_cl.reference', 'move_id', string='Referencias SII', copy=False,
    )
    tf_dte_cl_journal_document_type = fields.Selection(
        related='journal_id.tf_dte_cl_document_type', string='Tipo DTE del diario',
    )
    tf_dte_cl_is_dte = fields.Boolean(string='Es DTE', compute='_compute_tf_dte_cl_is_dte')

    @api.depends('move_type', 'journal_id.tf_dte_cl_document_type')
    def _compute_tf_dte_cl_is_dte(self):
        for move in self:
            move.tf_dte_cl_is_dte = bool(
                move.move_type in SALE_MOVE_TYPES and move.journal_id.tf_dte_cl_document_type
            )

    # ------------------------------------------------------------------
    # Implementación del mixin
    # ------------------------------------------------------------------
    def _tf_dte_cl_get_document_type(self):
        self.ensure_one()
        return self.tf_dte_cl_is_dte and self.journal_id.tf_dte_cl_document_type

    def _tf_dte_cl_get_branch(self):
        return self.journal_id.tf_dte_cl_branch_id

    def _tf_dte_cl_get_receiver(self):
        return self.partner_id

    def _tf_dte_cl_get_emission_date(self):
        # Antes de publicar, Odoo aún no completa invoice_date: usa la fecha de hoy.
        return self.invoice_date or fields.Date.context_today(self)

    def _tf_dte_cl_get_references(self):
        return self.tf_dte_cl_reference_ids

    def _tf_dte_cl_product_lines(self):
        return self.invoice_line_ids.filtered(lambda line: line.display_type == 'product')

    def _tf_dte_cl_line_infos(self) -> list[LineInfo]:
        infos = []
        for line in self._tf_dte_cl_product_lines():
            product = line.product_id
            product_name = product.name or (line.name or '').split('\n')[0]
            description = (line.name or '').strip()
            # Odoo propone "[código] nombre" como descripción: no se repite bajo el nombre.
            if description in (product_name, product.display_name, product.partner_ref):
                description = ''
            infos.append(LineInfo(
                label=(line.name or product_name or '').split('\n')[0][:60],
                product_name=product_name,
                description=description,
                default_code=line.product_id.default_code or '',
                quantity=line.quantity,
                uom=line.product_uom_id.name or '',
                price_unit=line.price_unit,
                discount=line.discount,
                subtotal=line.price_subtotal,
                taxes=[tax._tf_dte_cl_info() for tax in line.tax_ids],
            ))
        return infos

    def _tf_dte_cl_specific_errors(self) -> list[str]:
        self.ensure_one()
        _ = self.env._
        errors = []
        doc_type = self._tf_dte_cl_get_document_type()
        if self.move_type == 'out_refund' and doc_type != CREDIT_NOTE_DTE_TYPE:
            errors.append(_('una nota de crédito debe emitirse en un diario de notas de crédito (61)'))
        if self.move_type == 'out_invoice' and doc_type not in INVOICE_DTE_TYPES:
            errors.append(_('una factura o nota de débito no puede emitirse en un diario de notas de crédito'))
        if self.currency_id != self.company_id.currency_id:
            errors.append(_('el documento debe emitirse en la moneda de la compañía (CLP)'))
        if any(tax.amount_type == 'group' for tax in self._tf_dte_cl_product_lines().tax_ids):
            errors.append(_('los grupos de impuestos no están soportados en DTE; asigne los impuestos por separado'))
        errors += line_errors(doc_type, self._tf_dte_cl_line_infos())
        return errors

    def _tf_dte_cl_document_values(self) -> dict:
        self.ensure_one()
        infos = self._tf_dte_cl_line_infos()
        emission = self._tf_dte_cl_get_emission_date()
        due = self.invoice_date_due
        is_credit = bool(due and due > emission)
        id_doc = {'FmaPago': 2 if is_credit else 1}
        if is_credit:
            id_doc['FchVenc'] = due
        values = {'IdDoc': id_doc, 'Detalle': build_detail(infos)}
        rate = iva_rate(infos)
        if rate:
            values['TasaIVA'] = rate
        return values

    def _tf_dte_cl_expected_amounts(self) -> dict:
        self.ensure_one()
        net, exempt = split_amounts(self._tf_dte_cl_line_infos())
        by_code = defaultdict(float)
        for line in self.line_ids.filtered('tax_line_id'):
            by_code[line.tax_line_id.tf_dte_cl_sii_code] += line.balance
        iva = abs(by_code.pop(IVA_CODE, 0.0))
        additional = abs(sum(by_code.values()))
        return {
            'MntNeto': net,
            'MntExe': exempt,
            'MntIVA': iva,
            'ImptoReten': additional,
            'MntTotal': self.amount_total,
        }

    # ------------------------------------------------------------------
    # Impresión: Imprimir, Descargar y Enviar usan el formato DTE
    # ------------------------------------------------------------------
    def _get_name_invoice_report(self):
        self.ensure_one()
        if self.tf_dte_cl_folio:
            return 'tf_dte_cl.report_move_dte_document'
        return super()._get_name_invoice_report()

    # ------------------------------------------------------------------
    # Flujo contable
    # ------------------------------------------------------------------
    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        # Firma local (sin red): un error revierte la publicación y el folio vuelve al CAF.
        posted.filtered('tf_dte_cl_is_dte')._tf_dte_cl_prepare()
        return posted

    def button_draft(self):
        self.filtered('tf_dte_cl_state')._tf_dte_cl_cancel_dte()
        return super().button_draft()

    def button_cancel(self):
        self.filtered('tf_dte_cl_state')._tf_dte_cl_cancel_dte()
        return super().button_cancel()

    @api.ondelete(at_uninstall=False)
    def _unlink_except_tf_dte_cl(self):
        if self.filtered('tf_dte_cl_folio'):
            raise UserError(self.env._('No se puede eliminar un documento que tuvo folio SII.'))

    def _tf_dte_cl_credit_note_journal(self):
        self.ensure_one()
        journals = self.env['account.journal'].search([
            ('company_id', '=', self.company_id.id),
            ('type', '=', 'sale'),
            ('tf_dte_cl_document_type', '=', CREDIT_NOTE_DTE_TYPE),
        ])
        same_branch = journals.filtered(lambda j: j.tf_dte_cl_branch_id == self.journal_id.tf_dte_cl_branch_id)
        return (same_branch or journals)[:1]

    def _reverse_moves(self, default_values_list=None, cancel=False):
        """Nota de crédito desde un DTE aceptado: diario 61 y referencia al documento original."""
        default_values_list = list(default_values_list or [{} for _move in self])
        doc_types = self.env['tf_dte_cl.document_type']
        for move, default_values in zip(self, default_values_list):
            if move.tf_dte_cl_state not in ('accepted', 'accepted_objections'):
                continue
            journal = self.env['account.journal'].browse(default_values.get('journal_id'))
            if journal.tf_dte_cl_document_type != CREDIT_NOTE_DTE_TYPE:
                credit_journal = move._tf_dte_cl_credit_note_journal()
                if credit_journal:
                    default_values['journal_id'] = credit_journal.id
            doc_type = doc_types.search([('code', '=', move.tf_dte_cl_document_type)], limit=1)
            if doc_type and not default_values.get('tf_dte_cl_reference_ids'):
                default_values['tf_dte_cl_reference_ids'] = [fields.Command.create({
                    'document_type_id': doc_type.id,
                    'folio': str(move.tf_dte_cl_folio),
                    'date': move.tf_dte_cl_emission_date,
                    'code': CODE_CANCEL if cancel else CODE_FIX_AMOUNTS,
                    'reason': (default_values.get('ref') or move.name or '')[:90],
                })]
        return super()._reverse_moves(default_values_list=default_values_list, cancel=cancel)

    # ------------------------------------------------------------------
    # Crons
    # En Odoo 18 el cron de account_edi (ir_cron_edi_network) viene inactivo y
    # solo se dispara al publicar o cancelar: un DTE pendiente de reintento no se
    # volvería a enviar. Estos crons envían y consultan por su cuenta y dejan el
    # documento EDI coherente con el estado DTE.
    # ------------------------------------------------------------------
    def _tf_dte_cl_sync_edi_documents(self):
        for move in self:
            documents = move.edi_document_ids.filtered(
                lambda d: d.edi_format_id.code == DTE_CODE and d.state == 'to_send'
            )
            if not documents:
                continue
            if move.tf_dte_cl_state in SUCCESS_STATES:
                documents.write({'state': 'sent', 'error': False, 'blocking_level': False})
            elif move.tf_dte_cl_state == 'error':
                documents.write({'error': move.tf_dte_cl_error, 'blocking_level': 'error'})

    def _tf_dte_cl_send_and_sync(self):
        self._tf_dte_cl_send()
        self._tf_dte_cl_sync_edi_documents()

    def _tf_dte_cl_query_and_sync(self):
        self._tf_dte_cl_query()
        self._tf_dte_cl_sync_edi_documents()

    @api.model
    def _cron_tf_dte_cl_send(self, limit=50):
        moves = self.search([
            ('state', '=', 'posted'),
            ('tf_dte_cl_state', '=', 'signed'),
        ], order='id', limit=limit)
        moves._tf_dte_cl_run_each('_tf_dte_cl_send_and_sync')

    @api.model
    def _cron_tf_dte_cl_query(self):
        self._tf_dte_cl_cron_query(method_name='_tf_dte_cl_query_and_sync')
