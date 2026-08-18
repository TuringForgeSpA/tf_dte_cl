# tf_dte_cl

Núcleo B2B de facturación electrónica para Chile (SII) sobre Odoo 18, construido
sobre el framework nativo `account_edi`.

## Alcance

- Facturas, notas de crédito y notas de débito electrónicas → `account.edi.format`
  (envío asíncrono nativo `to_send -> sent`, timbrado y transmisión atómicos).
- Guías de despacho electrónicas → flujo asíncrono propio (`dte_send_state`),
  ya que `account.edi.format` está acoplado a `account.move`.
- Boletas electrónicas (B2C): **fuera de alcance**, se aborda en un módulo
  secundario dependiente de este.

## Dependencia externa

```
pip install facturacion_electronica pdf417gen pyOpenSSL
```

Se importa siempre de forma defensiva (`try/except ImportError`); si falta,
el módulo se instala igual pero los métodos de timbrado/envío devuelven un
error de bloqueo claro en vez de romper.

## Pendientes conocidos

- `data/res_comuna.csv` trae solo un subconjunto de comunas de la Región
  Metropolitana a modo de ejemplo. Antes de ir a producción, cárguelo con el
  listado oficial completo de las 346 comunas y verifique en su propia base
  los external IDs reales de `res.country.state` para Chile
  (`env['res.country.state'].search([('country_id.code', '=', 'CL')])`), ya
  que no se pudieron confirmar de forma fiable al momento de escribir este
  archivo.
- `DscRcgGlobal` (descuentos/recargos globales del DTE) queda como `TODO` en
  `account_move.py`: el módulo original nunca lo implementó.
