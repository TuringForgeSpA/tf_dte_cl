# -*- coding: utf-8 -*-
"""Impuestos SII y líneas de detalle del DTE (sin dependencias de Odoo).

Ruta real: models/dte_lines.py

Fuentes:
* SII, "Formato Documentos Tributarios Electrónicos" v2.4.2: máximo 60 líneas
  de detalle; tabla 4 "Codificación tipos de impuestos y recargos".
* facturacion_electronica 0.24.0: un único ``CodImpAdic`` por línea
  (documento.py, Detalle); el código 14 es su marca interna de IVA y la tasa se
  toma de ``TasaIVA``; una línea con tasa 0 debe llevar ``IndExe``.

Criterio tf_dte_cl: los precios se envían netos. Un impuesto con código SII no
puede estar incluido en el precio.
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

MAX_DETAIL_LINES = 60
IVA_CODE = '14'
EXEMPT_INDICATOR = 1        # IndExe 1: no afecto o exento de IVA

# Código SII → (descripción, tasa). IVA: DL 825, art. 14. Resto: tabla 4 del manual.
# El código 14 de la tabla oficial es "IVA de margen de comercialización"; la
# librería lo usa como IVA general y no lo informa como impuesto adicional.
SII_TAXES = OrderedDict([
    ('14', ('IVA', 19.0)),
    ('17', ('IVA anticipado faenamiento carne', 5.0)),
    ('18', ('IVA anticipado carne', 5.0)),
    ('24', ('Licores, piscos, whisky, aguardiente y vinos licorosos o aromatizados', 31.5)),
    ('25', ('Vinos', 20.5)),
    ('26', ('Cervezas y bebidas alcohólicas', 20.5)),
    ('27', ('Bebidas analcohólicas y minerales', 10.0)),
    ('271', ('Bebidas analcohólicas y minerales con elevado contenido de azúcares', 18.0)),
])
SII_TAX_SELECTION = [(code, '%s - %s (%s%%)' % (code, name, rate)) for code, (name, rate) in SII_TAXES.items()]
ADDITIONAL_CODES = frozenset(SII_TAXES) - {IVA_CODE}


@dataclass
class TaxInfo:
    name: str
    code: str | None
    rate: float
    amount_type: str = 'percent'
    price_include: bool = False
    include_base_amount: bool = False
    type_tax_use: str = 'sale'


@dataclass
class LineInfo:
    label: str
    product_name: str
    description: str = ''
    default_code: str = ''
    quantity: float = 0.0
    uom: str = ''
    price_unit: float = 0.0
    discount: float = 0.0
    subtotal: float = 0.0
    taxes: list[TaxInfo] = field(default_factory=list)

    @property
    def iva(self) -> list[TaxInfo]:
        return [tax for tax in self.taxes if tax.code == IVA_CODE]

    @property
    def additional(self) -> list[TaxInfo]:
        return [tax for tax in self.taxes if tax.code in ADDITIONAL_CODES]

    @property
    def is_exempt(self) -> bool:
        return not self.taxes


def round_half_up(value) -> int:
    return int(Decimal(str(value)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def tax_config_errors(tax: TaxInfo) -> list[str]:
    """Errores de configuración de un impuesto con código SII."""
    if not tax.code:
        return []
    errors = []
    if tax.code not in SII_TAXES:
        return ['el código SII %s no está soportado' % tax.code]
    if tax.type_tax_use != 'sale':
        errors.append('debe ser un impuesto de ventas')
    if tax.amount_type != 'percent':
        errors.append('debe ser un impuesto porcentual')
    if tax.price_include:
        errors.append('no puede estar incluido en el precio')
    if tax.include_base_amount:
        errors.append('no puede afectar la base de otros impuestos')
    expected = SII_TAXES[tax.code][1]
    if abs(tax.rate - expected) > 0.0001:
        errors.append('su tasa debe ser %s%% para el código %s' % (expected, tax.code))
    return errors


def line_errors(doc_type: str, lines: list[LineInfo]) -> list[str]:
    """Errores de las líneas de detalle para un tipo de DTE."""
    errors = []
    if not lines:
        errors.append('el documento no tiene líneas de detalle')
    if len(lines) > MAX_DETAIL_LINES:
        errors.append('el DTE admite hasta %s líneas de detalle' % MAX_DETAIL_LINES)
    for line in lines:
        prefix = 'línea "%s": ' % line.label
        if line.quantity <= 0:
            errors.append(prefix + 'la cantidad debe ser mayor que 0')
        if line.price_unit < 0:
            errors.append(prefix + 'el precio no puede ser negativo (use el descuento de la línea)')
        if not 0 <= line.discount <= 100:
            errors.append(prefix + 'el descuento debe estar entre 0 y 100%')
        for tax in line.taxes:
            if not tax.code:
                errors.append(prefix + 'el impuesto "%s" no tiene código SII' % tax.name)
                continue
            errors += [prefix + 'impuesto "%s": %s' % (tax.name, error) for error in tax_config_errors(tax)]
        if len(line.iva) > 1:
            errors.append(prefix + 'tiene más de un IVA')
        if len(line.additional) > 1:
            errors.append(prefix + 'el DTE admite un solo impuesto adicional por línea')
        if line.additional and not line.iva:
            errors.append(prefix + 'un impuesto adicional requiere IVA en la misma línea')
        if doc_type == '34' and line.taxes:
            errors.append(prefix + 'una factura exenta (34) no lleva impuestos')
    if doc_type == '33' and lines and not any(line.iva for line in lines):
        errors.append('una factura electrónica (33) debe tener al menos una línea con IVA; '
                      'si todo es exento, use una factura exenta (34)')
    return errors


def iva_rate(lines: list[LineInfo]) -> float | None:
    rates = {tax.rate for line in lines for tax in line.iva}
    return rates.pop() if len(rates) == 1 else None


def build_detail(lines: list[LineInfo]) -> list[OrderedDict]:
    """Bloque ``Detalle`` para facturacion_electronica.

    El orden importa: ``IndExe`` debe asignarse antes que ``Impuesto``, y una
    línea exenta no lleva la clave ``Impuesto`` (así suma al monto exento).
    """
    detail = []
    for number, line in enumerate(lines, start=1):
        item = OrderedDict()
        item['NroLinDet'] = number
        if line.default_code:
            item['CdgItem'] = [{'TpoCodigo': 'INT1', 'VlrCodigo': line.default_code[:35]}]
        if line.is_exempt:
            item['IndExe'] = EXEMPT_INDICATOR
        item['NmbItem'] = (line.product_name or line.description or line.label)[:80]
        if line.description and line.description != line.product_name:
            item['DscItem'] = line.description[:1000]
        item['QtyItem'] = line.quantity
        if line.uom:
            item['UnmdItem'] = line.uom[:4]
        price = round(line.price_unit, 4)
        item['PrcItem'] = price
        amount = round_half_up(line.subtotal)
        if line.discount:
            discount_amount = round_half_up(Decimal(str(line.quantity)) * Decimal(str(price))) - amount
            if discount_amount > 0:
                item['DescuentoPct'] = round(line.discount, 2)
                item['DescuentoMonto'] = discount_amount
        item['MontoItem'] = amount
        if not line.is_exempt:
            ordered = line.iva + line.additional
            item['Impuesto'] = [{'CodImp': int(tax.code), 'TasaImp': tax.rate} for tax in ordered]
        detail.append(item)
    return detail


def split_amounts(lines: list[LineInfo]) -> tuple[int, int]:
    """(monto neto afecto, monto exento) tal como los calcula la librería."""
    net = sum(round_half_up(line.subtotal) for line in lines if line.iva)
    exempt = sum(round_half_up(line.subtotal) for line in lines if line.is_exempt)
    return net, exempt


def compute_totals(lines: list[LineInfo]) -> dict:
    """Totales del DTE con la misma regla de la librería: cada impuesto se calcula
    sobre la suma de los montos de línea redondeados y se redondea una vez."""
    net, exempt = split_amounts(lines)
    bases = defaultdict(int)
    for line in lines:
        for tax in line.iva + line.additional:
            bases[(tax.code, tax.rate)] += round_half_up(line.subtotal)
    taxes = OrderedDict()
    for (code, rate), base in sorted(bases.items(), key=lambda item: (item[0][0] != IVA_CODE, item[0][0])):
        taxes[code] = (rate, round_half_up(Decimal(base) * Decimal(str(rate)) / 100))
    total = net + exempt + sum(amount for _rate, amount in taxes.values())
    return {'net': net, 'exempt': exempt, 'taxes': taxes, 'total': total}
