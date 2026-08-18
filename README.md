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

## Catálogo de comunas

`data/res_comuna_seed.csv` trae las 346 comunas oficiales de Chile (código INE,
nombre, región). Se carga mediante un `post_init_hook` (`hooks.py`), no como
CSV de datos estándar: al instalar, el hook empareja cada comuna con su
`res.country.state` **por nombre normalizado** (sin tildes, insensible a
mayúsculas) en vez de por external ID, porque el external ID exacto de cada
región de Chile en el módulo `base` no está documentado de forma estable
entre versiones de Odoo — un solo external ID incorrecto habría hecho fallar
la carga completa del catálogo.

Si alguna región no logra emparejarse, la comuna igual se crea (sin
`state_id`) y queda registrada en el log del servidor con el detalle exacto;
nunca bloquea la instalación. Después de instalar, conviene revisar el log en
busca de la línea `Catálogo de comunas: no se pudo emparejar...` — si aparece,
completar el `state_id` faltante manualmente desde Ajustes > Técnico > Comunas
para esa región puntual.

## Pendientes conocidos
- `DscRcgGlobal` (descuentos/recargos globales del DTE) queda como `TODO` en
  `account_move.py`: el módulo original nunca lo implementó.
