# -*- coding: utf-8 -*-
"""Formato de papel compacto para imprimir DTE con los reportes estándar de facturas y entregas.

Ruta real: models/ir_actions_report.py

El reporte estándar (Imprimir, Descargar, Enviar) usa el formato de papel de la
compañía, que reserva el margen superior para el encabezado de Odoo. El DTE
lleva su propio encabezado en el cuerpo, así que, cuando todos los documentos a
imprimir son DTE, se usa el formato compacto del módulo.
"""
from odoo import models

# Reportes estándar que, con documentos DTE, muestran el formato del SII.
STANDARD_DTE_REPORTS = {
    'account.move': ('account.report_invoice', 'account.report_invoice_with_payments'),
    'stock.picking': ('stock.report_deliveryslip',),
}
DTE_PAPERFORMAT_XMLID = 'tf_dte_cl.paperformat_tf_dte_cl_a4'


class IrActionsReport(models.Model):
    _inherit = 'ir.actions.report'

    def _render_qweb_pdf(self, report_ref, res_ids=None, data=None):
        report = self._get_report(report_ref)
        if report.report_name in STANDARD_DTE_REPORTS.get(report.model, ()) and res_ids:
            ids = [res_ids] if isinstance(res_ids, int) else list(res_ids)
            records = self.env[report.model].browse(ids).exists()
            if records and all(records.mapped('tf_dte_cl_folio')):
                self = self.with_context(tf_dte_cl_dte_paperformat=True)
        return super()._render_qweb_pdf(report_ref, res_ids=res_ids, data=data)

    def get_paperformat(self):
        if self.env.context.get('tf_dte_cl_dte_paperformat') and self.model in STANDARD_DTE_REPORTS:
            paperformat = self.env.ref(DTE_PAPERFORMAT_XMLID, raise_if_not_found=False)
            if paperformat:
                return paperformat
        return super().get_paperformat()
