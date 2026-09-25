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


@dataclass
class GlobalAdjustment:
    """Descuento (monto negativo) o recargo (positivo) global, en pesos."""
    label: str
    amount: int
    taxes: list[TaxInfo] = field(default_factory=list)

    @property
    def is_exempt(self) -> bool:
        return not self.taxes


def split_amounts(lines: list[LineInfo], adjustments=()) -> tuple[int, int]:
    """(monto neto afecto, monto exento), incluidos los descuentos y recargos globales."""
    net = sum(round_half_up(line.subtotal) for line in lines if line.iva)
    exempt = sum(round_half_up(line.subtotal) for line in lines if line.is_exempt)
    net += sum(adj.amount for adj in adjustments if not adj.is_exempt)
    exempt += sum(adj.amount for adj in adjustments if adj.is_exempt)
    return net, exempt


def adjustment_errors(doc_type: str, adjustments, lines: list[LineInfo]) -> list[str]:
    """Errores de los descuentos y recargos globales (DscRcgGlobal en pesos).

    La librería resta un descuento global en pesos por separado de cada total,
    incluido el IVA y los impuestos adicionales. El módulo informa neto, IVA y
    total explícitos para corregir el IVA, pero no puede corregir los impuestos
    adicionales: por eso no se admiten en esos documentos.
    """
    errors = []
    adjustments = [adj for adj in adjustments if adj.amount]
    if not adjustments:
        return errors
    for adj in adjustments:
        prefix = 'descuento o recargo global "%s": ' % adj.label
        if adj.taxes and (len(adj.taxes) > 1 or adj.taxes[0].code != IVA_CODE):
            errors.append(prefix + 'solo puede llevar IVA (afecto) o ningún impuesto (exento)')
        for tax in adj.taxes:
            errors += [prefix + error for error in tax_config_errors(tax)]
        if doc_type == '34' and adj.taxes:
            errors.append(prefix + 'en una factura exenta (34) no puede llevar IVA')
    if any(line.additional for line in lines) and any(not adj.is_exempt for adj in adjustments):
        errors.append('no se admiten descuentos o recargos globales afectos en documentos con impuestos '
                      'adicionales; aplique el descuento en cada línea')
    net_lines = sum(round_half_up(line.subtotal) for line in lines if line.iva)
    exempt_lines = sum(round_half_up(line.subtotal) for line in lines if line.is_exempt)
    net, exempt = split_amounts(lines, adjustments)
    if any(not adj.is_exempt for adj in adjustments) and not net_lines:
        errors.append('un descuento o recargo global afecto requiere líneas afectas')
    if any(adj.is_exempt for adj in adjustments) and not exempt_lines:
        errors.append('un descuento o recargo global exento requiere líneas exentas')
    if net < 0 or exempt < 0:
        errors.append('los descuentos globales no pueden superar el monto de las líneas')
    return errors


def build_global_adjustments(adjustments) -> list[OrderedDict]:
    """Bloque DscRcgGlobal para la librería (valores en pesos)."""
    result = []
    for number, adj in enumerate((a for a in adjustments if a.amount), start=1):
        item = OrderedDict()
        item['NroLinDR'] = number
        item['TpoMov'] = 'D' if adj.amount < 0 else 'R'
        item['GlosaDR'] = (adj.label or ('Descuento global' if adj.amount < 0 else 'Recargo global'))[:45]
        item['TpoValor'] = '$'
        item['ValorDR'] = abs(adj.amount)
        if adj.is_exempt:
            item['IndExeDR'] = 1
        result.append(item)
    return result


def compute_totals(lines: list[LineInfo], adjustments=()) -> dict:
    """Totales del DTE: cada impuesto se calcula sobre la suma de los montos de línea
    redondeados (más los descuentos y recargos globales afectos, en el IVA) y se
    redondea una vez."""
    net, exempt = split_amounts(lines, adjustments)
    bases = defaultdict(int)
    for line in lines:
        for tax in line.iva + line.additional:
            bases[(tax.code, tax.rate)] += round_half_up(line.subtotal)
    affected = sum(adj.amount for adj in adjustments if not adj.is_exempt)
    if affected:
        iva_keys = [key for key in bases if key[0] == IVA_CODE]
        if iva_keys:
            bases[iva_keys[0]] += affected
    taxes = OrderedDict()
    for (code, rate), base in sorted(bases.items(), key=lambda item: (item[0][0] != IVA_CODE, item[0][0])):
        taxes[code] = (rate, round_half_up(Decimal(base) * Decimal(str(rate)) / 100))
    total = net + exempt + sum(amount for _rate, amount in taxes.values())
    return {'net': net, 'exempt': exempt, 'taxes': taxes, 'total': total}
